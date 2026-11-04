"""`geo`: measure the spiral geometry of a burned DVD from read timing ("ladder").

Under constant rotation, the spindle phase of a target read's completion relative to
a reference read's is the track angle between the ends of the two ECC blocks, plus a
constant c (decode latency difference, ideally ~0):

    phi(X) = Theta(n_X) - Theta(n_R) + c  (mod 360),   Theta(n) = 2n / (S + u),  S = sqrt(u^2 + v n / pi)

with n the channel bit at the end of the block and u = r0/b, v = p/b as in calib.py.
Only the phase modulo 360 is measured, and the nominal geometry mispredicts the angle
between distant sectors by thousands of turns, so targets are taken as a ladder:

  1. Settle the spindle (repeat the reference until its period stops changing).
  2. Measure a target one or two turns from the reference: the nominal model predicts
     it to within a few degrees, which fixes c.
  3. Fit u, v, c (with loose priors on u and v) and take as the next target the
     farthest candidate whose predicted phase is uncertain by less than SAFE_DEG; its
     whole-turn count follows from the prediction. Refit and repeat.

Each new rung shrinks the rate uncertainty, so the reach grows geometrically and some
twenty targets span the readable radius. Every target block re-measures the reference
period; a block taken while the spindle speed changed is retried after settling.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics

from . import analysis, linalg
from .calib import SECTOR_BITS, CalLayout, _dtheta, _report, theta
from .timing import TimingProbe

SAFE_DEG = 25.0
TURN_ABORT_DEG = 90.0       # any rung this far from the refit means a likely whole-turn error
CHI2_ABORT = 25.0           # healthy runs: 0.3-2 simulated, 0.27-0.64 on real discs
MIN_SIGMA_DEG = 0.2         # floor on a target's phase uncertainty (systematics)
# Reference period must match the settled one this closely. Each block's phases use
# that block's own period fit, so this only rejects blocks where the speed jumped. The
# GH70N spins 0.03-0.07 % slower while seeking back and forth than while settling.
PERIOD_TOL = 2e-3
FIRST_TURNS = (1.5, 4.0)    # first rungs, in turns from the reference
SETTLE_READS = 16
MIN_SPACING_SECTORS = 4000  # about a third of a millimetre of radius


class Ladder:
    def __init__(self, dev, ranges=None, r0_mm=24.0, pitch_um=0.74, bit_nm=133.3, reps=24,
                 max_targets=22, sigma_u=0.01, sigma_v=0.03, speed_kbs=5540, ref_lba=None,
                 host_delay_ms=25.0, log=print, seed=1):
        self.dev = dev
        self.ranges_arg = ranges
        self.prior = CalLayout(r0_mm=r0_mm, pitch_um=pitch_um, bit_nm=bit_nm)
        self.u0, self.v0 = self.prior.u0, self.prior.v0
        self.sig_u, self.sig_v = sigma_u * self.u0, sigma_v * self.v0
        self.reps = reps
        self.max_targets = max_targets
        self.ref_arg = ref_lba
        self.log = log
        self.tp = TimingProbe(dev, log=lambda *_: None, reps=reps, speed_kbs=speed_kbs,
                              host_delay_ms=host_delay_ms, seed=seed)
        self.targets: list[dict] = []
        self.u, self.v, self.c = self.u0, self.v0, 0.0
        self.cov = None

    # ------------------------------------------------------------ geometry helpers

    def _n_end(self, lba: int) -> float:
        """Channel bit at the end of the ECC block holding lba (where the read completes)."""
        return float((lba // 16 + 1) * 16 * SECTOR_BITS)

    def _lba_at_turns(self, turns: float) -> int:
        th = theta(self.n_ref, self.u, self.v) + 2 * math.pi * turns
        s = self.u + self.v * th / (2 * math.pi)
        n = math.pi * (s * s - self.u * self.u) / self.v
        return int(n // SECTOR_BITS) // 16 * 16

    def _predict(self, n):
        return float(theta(n, self.u, self.v) - theta(self.n_ref, self.u, self.v)) + self.c

    def _sigma_pred_deg(self, n) -> float:
        if self.cov is None:
            return float("inf")
        du, dv = _dtheta(n, self.u, self.v)
        du0, dv0 = _dtheta(self.n_ref, self.u, self.v)
        g = [du - du0, dv - dv0, 1.0]
        return math.degrees(math.sqrt(max(linalg.quad(g, self.cov), 0.0)))

    def _span_variance_after(self, lba: int) -> float:
        """Variance of the accumulated angle across the readable range if lba were measured
        next with the typical per-rung uncertainty (rank-one covariance update)."""
        lo = self._n_end(min(a for a, _ in self.ranges))
        hi = self._n_end(max(b for _, b in self.ranges) - 16)
        du_lo, dv_lo = _dtheta(lo, self.u, self.v)
        du_hi, dv_hi = _dtheta(hi, self.u, self.v)
        g = [du_hi - du_lo, dv_hi - dv_lo, 0.0]
        n = self._n_end(lba)
        du, dv = _dtheta(n, self.u, self.v)
        du0, dv0 = _dtheta(self.n_ref, self.u, self.v)
        h = [du - du0, dv - dv0, 1.0]
        s2 = math.radians(statistics.median([t["sigma_deg"] for t in self.targets])) ** 2
        # rank-one covariance update, evaluated as a quadratic form rather than by
        # forming the updated matrix: g C g - (g C h)^2 / (s2 + h C h)
        ch = linalg.mat_vec(self.cov, h)
        denom = s2 + sum(hi * ci for hi, ci in zip(h, ch))
        return linalg.quad(g, self.cov) - sum(gi * ci for gi, ci in zip(g, ch)) ** 2 / denom

    def _allowed(self, lba: int) -> int | None:
        """Nearest block-aligned LBA inside the readable ranges."""
        best = None
        for a, b in self.ranges:
            x = min(max(lba, a), b - 16)
            x -= x % 16
            if best is None or abs(x - lba) < abs(best - lba):
                best = x
        return best

    # ------------------------------------------------------------ measurement

    def _ref_period(self, times):
        return analysis.estimate_period(times, prior_s=self.T, prior_window=0.02) if self.T else \
            analysis.estimate_period(times)

    def settle(self, max_rounds: int = 10) -> float:
        tp, prev = self.tp, None
        self.T = None
        for i in range(max_rounds):
            times = [r.t_end_ns for r in (tp._measure(self.ref, "settle") for _ in range(SETTLE_READS))
                     if tp._usable(r)]
            fit = analysis.estimate_period(times) if len(times) >= 8 else None
            if fit and fit.coherence > 0.99 and prev and abs(fit.period_s / prev - 1) < PERIOD_TOL:
                self.T = fit.period_s
                self.log(f"Spindle settled: T = {fit.period_s * 1e3:.4f} ms ({60 / fit.period_s:.1f} rpm), "
                         f"jitter {fit.residual_std_s * 1e6:.1f} us")
                return self.T
            prev = fit.period_s if fit and fit.coherence > 0.99 else None
        raise RuntimeError("the spindle speed never settled; is the read speed fixed (--speed)?")

    def measure(self, lba: int) -> dict | None:
        tp = self.tp
        ref_t, x_t = [], []
        for _ in range(self.reps):
            r = tp._measure(self.ref, "ref")
            if tp._usable(r):
                ref_t.append(r.t_end_ns)
            x = tp._measure(lba, "target")
            if tp._usable(x):
                x_t.append(x.t_end_ns)
        fit = self._ref_period(ref_t) if len(ref_t) >= 8 else None
        if not fit or fit.coherence < 0.98 or abs(fit.period_s / self.T - 1) > PERIOD_TOL or len(x_t) < 8:
            return None
        ph = analysis.robust_circular_mean(analysis.phase_offsets(fit, x_t))
        if ph["mean_deg"] is None or ph["std_deg"] > 20:
            return None
        self.T = fit.period_s  # follow slow drift of the regulated speed
        return {"lba": lba, "n": self._n_end(lba), "phase_deg": ph["mean_deg"],
                "sigma_deg": max(ph["sem_deg"], MIN_SIGMA_DEG), "std_deg": ph["std_deg"],
                "used": ph["used"], "period_ms": round(fit.period_s * 1e3, 5)}

    def _measure_retry(self, lba: int) -> dict | None:
        m = self.measure(lba)
        if m is None:
            self.log(f"  LBA {lba}: spindle unsteady or reads failing; settling and retrying")
            self.settle()
            m = self.measure(lba)
        return m

    # ------------------------------------------------------------ fitting

    def fit(self, iters: int = 15) -> None:
        """Weighted Gauss-Newton on the unwrapped rung angles, with loose priors on u, v.

        Three unknowns against a dozen or more rungs, so forming the normal equations
        by hand costs nothing and keeps the tool free of numpy.
        """
        u, v, c = self.u, self.v, self.c
        n = [t["n"] for t in self.targets]
        y = [math.radians(t["unwrapped_deg"]) for t in self.targets]
        w = [1.0 / math.radians(t["sigma_deg"]) for t in self.targets]
        th_ref = theta(self.n_ref, u, v)
        rows: list[list[float]] = []
        rhs: list[float] = []
        scales: list[float] = []
        for _ in range(iters):
            th_ref = theta(self.n_ref, u, v)
            du0, dv0 = _dtheta(self.n_ref, u, v)
            rows, rhs = [], []
            for ni, yi, wi in zip(n, y, w):
                du, dv = _dtheta(ni, u, v)
                rows.append([(du - du0) * wi, (dv - dv0) * wi, wi])
                rhs.append((yi - (theta(ni, u, v) - th_ref + c)) * wi)
            rows.append([1.0 / self.sig_u, 0.0, 0.0])
            rhs.append((self.u0 - u) / self.sig_u)
            rows.append([0.0, 1.0 / self.sig_v, 0.0])
            rhs.append((self.v0 - v) / self.sig_v)
            step, scales = linalg.lstsq(rows, rhs)
            step = [x / sc for x, sc in zip(step, scales)]
            u, v, c = u + step[0], v + step[1], c + step[2]
            if all(abs(x) < 1e-9 for x in step):
                break
        th_ref = theta(self.n_ref, u, v)
        model = [theta(ni, u, v) - th_ref + c for ni in n]
        res = [(yi - mi) * wi for yi, mi, wi in zip(y, model, w)]
        chi2 = sum(r * r for r in res)
        dof = max(1, len(n) - 3)
        scaled = [[row[i] / scales[i] for i in range(3)] for row in rows]
        inv = linalg.inverse(linalg.gram(scaled)) or [[float("nan")] * 3 for _ in range(3)]
        cov = [[inv[i][j] / (scales[i] * scales[j]) for j in range(3)] for i in range(3)]
        self.u, self.v, self.c = float(u), float(v), float(c)
        self.cov = [[x * max(1.0, chi2 / dof) for x in row] for row in cov]
        self.chi2_dof = chi2 / dof
        for t, m in zip(self.targets, model):
            t["model_deg"] = math.degrees(m)
            t["residual_deg"] = t["unwrapped_deg"] - t["model_deg"]

    # ------------------------------------------------------------ the ladder

    def _candidates(self) -> list[int]:
        lo = min(a for a, _ in self.ranges)
        hi = max(b for _, b in self.ranges)
        grid = [int(lo + (hi - 16 - lo) * i / 39) for i in range(40)]
        turns = [s * 2 ** k for k in range(0, 18) for s in (1, -1)]
        near = [self._lba_at_turns(t) for t in turns]
        out = set()
        for lba in list(grid) + near:
            x = self._allowed(int(lba))
            if x is not None and abs(x - self.ref) >= 16:
                out.add(x)
        return sorted(out)

    def _add(self, m: dict, predicted_deg: float) -> None:
        k = round((predicted_deg - m["phase_deg"]) / 360.0)
        m["unwrapped_deg"] = m["phase_deg"] + 360.0 * k
        m["predicted_deg"] = predicted_deg
        self.targets.append(m)

    def run(self) -> dict:
        tp = self.tp
        tp.setup()
        tp.cache_test()
        if tp.strategy not in ("fua", "plain"):
            self.log(f"warning: cache strategy {tp.strategy}; phases will be noisier")
        last = tp.last_lba
        if self.ranges_arg:
            self.ranges = [(max(0, a), min(b, last + 1)) for a, b in self.ranges_arg]
        else:
            self.ranges = [(int(last * 0.03), int(last * 0.85))]
        self.ranges = [(a - a % 16, b - b % 16) for a, b in self.ranges if b - a > 64]
        if self.ref_arg is not None:
            self.ref = self._allowed(self.ref_arg)
        else:
            mid = (min(a for a, _ in self.ranges) + max(b for _, b in self.ranges)) // 2
            self.ref = self._allowed(mid)
        self.n_ref = self._n_end(self.ref)
        self.log(f"Reference LBA {self.ref}; readable ranges {self.ranges}")
        self.settle()

        # First rungs: a turn or two away, where the nominal model is good to a few degrees.
        for i, turns in enumerate(FIRST_TURNS):
            lba = self._allowed(self._lba_at_turns(turns))
            m = self._measure_retry(lba)
            if m is None:
                raise RuntimeError(f"could not measure LBA {lba} near the reference")
            pred = math.degrees(self._predict(m["n"]))
            if i == 0:
                self.c = math.radians(((m["phase_deg"] - pred + 180) % 360) - 180)
                pred = math.degrees(self._predict(m["n"]))
            self._add(m, pred)
            self.log(f"  rung {len(self.targets):2d}: LBA {lba:>8} ({turns:+.1f} turns) phase {m['phase_deg']:8.3f} "
                     f"+/- {m['sigma_deg']:.3f} deg")
        self.fit()

        tried = {t["lba"] for t in self.targets}
        while len(self.targets) < self.max_targets:
            # Skip candidates next to one already measured: a second point there adds little.
            cands = [c for c in self._candidates()
                     if c not in tried and min(abs(c - t) for t in tried) >= MIN_SPACING_SECTORS]
            if not cands:
                break
            sig = {c: self._sigma_pred_deg(self._n_end(c)) for c in cands}
            safe = [c for c in cands if sig[c] < SAFE_DEG]
            if safe and len(safe) == len(cands):
                # Reach is no longer the limit. Most rungs go where they shrink the uncertainty
                # of the angle across the whole range the most (that favours the ends); every
                # third fills the widest gap, so the middle is checked too (the first real run
                # left 34.6-42.6 and 44-49 mm empty).
                if len(self.targets) % 3 == 0:
                    lba = max(safe, key=lambda c: min(abs(c - t) for t in tried | {self.ref}))
                else:
                    lba = min(safe, key=self._span_variance_after)
            elif safe:
                lba = max(safe, key=lambda c: abs(self._n_end(c) - self.n_ref))
            else:
                lba = min(cands, key=lambda c: sig[c])
            tried.add(lba)
            pred = math.degrees(self._predict(self._n_end(lba)))
            m = self._measure_retry(lba)
            if m is None:
                self.log(f"  LBA {lba}: skipped (no stable measurement)")
                continue
            self._add(m, pred)
            self.fit()
            t = self.targets[-1]
            self.log(f"  rung {len(self.targets):2d}: LBA {lba:>8} predicted +/- {sig[lba]:6.2f} deg, "
                     f"whole turns {round((t['unwrapped_deg']) / 360):>7}, residual {t['residual_deg']:+7.3f} deg, "
                     f"chi2/dof {self.chi2_dof:.2f}")
            # A wrong whole-turn count is partly absorbed by u and v, so the new rung itself can
            # look fine; it shows as an exploding chi2 and large residuals elsewhere. Stop rather
            # than fit through it (simulated: chi2/dof jumps from ~1 to ~360 at the bad rung).
            worst = max(abs(x["residual_deg"]) for x in self.targets)
            if self.chi2_dof > CHI2_ABORT or worst > TURN_ABORT_DEG:
                raise RuntimeError(f"after the rung at LBA {lba}: chi2/dof {self.chi2_dof:.1f}, worst residual "
                                   f"{worst:.1f} deg; a whole-turn count is probably wrong, so the fit was stopped")
        return self.result()

    def result(self) -> dict:
        rep = _report(self.u, self.v, self.cov, (self.u, self.v), self.prior, None)
        n_meas = [t["n"] for t in self.targets] + [self.n_ref]
        lo, hi = min(n_meas), max(n_meas)
        du_lo, dv_lo = _dtheta(lo, self.u, self.v)
        du_hi, dv_hi = _dtheta(hi, self.u, self.v)
        g = [du_hi - du_lo, dv_hi - dv_lo]
        b_mm = self.prior.bit_nm * 1e-6
        r_of = lambda n: b_mm * math.sqrt(self.u ** 2 + self.v * n / math.pi)
        rep["measured_span_mm"] = [round(r_of(lo), 3), round(r_of(hi), 3)]
        sub = [row[:2] for row in self.cov[:2]]
        rep["angle_sigma_measured_span_deg"] = math.degrees(math.sqrt(max(linalg.quad(g, sub), 0.0)))
        rep["c_deg"] = math.degrees(self.c)
        rep["chi2_per_dof"] = self.chi2_dof
        rep["residual_rms_deg"] = math.sqrt(sum(t["residual_deg"] ** 2 for t in self.targets)
                                            / len(self.targets))
        rep["period_ms"] = self.T * 1e3
        rep["reference_lba"] = self.ref
        return rep


def write_outputs(ladder: Ladder, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "targets.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["lba", "n_bits", "phase_deg", "sigma_deg", "std_deg", "used", "predicted_deg",
                    "unwrapped_deg", "model_deg", "residual_deg", "period_ms"])
        for t in ladder.targets:
            w.writerow([t["lba"], f"{t['n']:.0f}", t["phase_deg"], f"{t['sigma_deg']:.3f}", t["std_deg"], t["used"],
                        f"{t['predicted_deg']:.3f}", f"{t['unwrapped_deg']:.3f}", f"{t['model_deg']:.3f}",
                        f"{t['residual_deg']:.3f}", t["period_ms"]])
    res = ladder.result()
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"fit": res, "cache": ladder.tp.summary.get("cache"),
                   "setup": ladder.tp.summary.get("setup"),
                   "set_cd_speed": ladder.tp.summary.get("set_cd_speed")}, f, indent=2, default=float)
    events = ladder.tp.events_as_rows()
    if events:
        with open(os.path.join(out_dir, "events.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(events[0]))
            w.writeheader()
            w.writerows(events)
