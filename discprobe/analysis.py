"""Timing analysis: spindle period from completion times, and phase between sectors.

Completion times of reads of one sector are, apart from jitter, integer multiples of
the revolution period T: the drive can only return the sector after it has passed
under the head. A Rayleigh periodogram over those times finds T without knowing how
many revolutions separate consecutive reads, which is what lets seeks and cache
eviction reads sit between them.
"""

from __future__ import annotations

import cmath
import math
import statistics
from dataclasses import dataclass, field

TWO_PI = 2.0 * math.pi
HARMONIC_ACCEPT = 0.9


def coherence(times_s: list[float], freq_hz: float) -> float:
    w = TWO_PI * freq_hz
    acc = 0j
    for t in times_s:
        acc += cmath.exp(1j * w * t)
    return abs(acc) / len(times_s)


def _best_in(times_s, f_lo, f_hi, df):
    steps = max(2, int((f_hi - f_lo) / df) + 1)
    best_f, best_c = f_lo, -1.0
    for i in range(steps):
        f = f_lo + (f_hi - f_lo) * i / (steps - 1)
        c = coherence(times_s, f)
        if c > best_c:
            best_f, best_c = f, c
    return best_f, best_c


def _refine(times_s, f, df):
    for _ in range(3):
        f, _ = _best_in(times_s, f - df, f + df, df / 20)
        df /= 10
    return f, coherence(times_s, f)


@dataclass
class PeriodFit:
    period_s: float
    coherence: float
    base_ns: int
    t0_s: float
    residual_std_s: float
    used: int
    total: int
    residuals_s: list[float | None] = field(repr=False)
    times_s: list[float] = field(repr=False)

    @property
    def rpm(self) -> float:
        return 60.0 / self.period_s

    @property
    def jitter_deg(self) -> float:
        return 360.0 * self.residual_std_s / self.period_s

    def summary(self) -> dict:
        return {"period_ms": round(self.period_s * 1e3, 5), "rpm": round(self.rpm, 2),
                "coherence": round(self.coherence, 4), "jitter_us": round(self.residual_std_s * 1e6, 2),
                "jitter_deg": round(self.jitter_deg, 3), "used": self.used, "total": self.total}


def estimate_period(times_ns: list[int], t_min_s: float = 0.003, t_max_s: float = 0.4,
                    prior_s: float | None = None, prior_window: float = 0.05) -> PeriodFit | None:
    if len(times_ns) < 5:
        return None
    base = times_ns[0]
    t = [(x - base) * 1e-9 for x in times_ns]
    span = t[-1] - t[0]
    if span <= 0:
        return None
    df = 1.0 / (8.0 * span)

    candidates = []
    if prior_s:
        # The prior may itself be a multiple of the true period (a drive that needs two
        # revolutions to re-read), so also look at its first sub-multiples.
        for k in (1, 2, 3):
            centre = k / prior_s
            f, c = _best_in(t, centre * (1 - prior_window), centre * (1 + prior_window), df)
            candidates.append(_refine(t, f, df))
    else:
        f_lo, f_hi = 1.0 / t_max_s, 1.0 / t_min_s
        if (f_hi - f_lo) / df > 200000:
            df = (f_hi - f_lo) / 200000
        f_peak, _ = _best_in(t, f_lo, f_hi, df)
        f_best, c_best = _refine(t, f_peak, df)
        candidates.append((f_best, c_best))
        # Completion times that are multiples of T are also multiples of T/k, so the
        # periodogram peaks equally at harmonics. The fundamental is the lowest one.
        for k in range(2, 13):
            fk = f_best / k
            if fk < f_lo:
                break
            candidates.append(_refine(t, fk, df))

    c_max = max(c for _, c in candidates)
    f = min(f for f, c in candidates if c >= HARMONIC_ACCEPT * c_max)
    c = coherence(t, f)
    period = 1.0 / f

    # Least-squares fit t = t0 + n*T on the revolution numbers, with outliers removed.
    keep = list(range(len(t)))
    t0 = 0.0
    residuals: list[float | None] = [None] * len(t)
    for _ in range(3):
        ns = [round((t[i] - t0) / period) for i in keep]
        n_mean = statistics.fmean(ns)
        t_mean = statistics.fmean(t[i] for i in keep)
        sxx = sum((n - n_mean) ** 2 for n in ns)
        if sxx > 0:
            period = sum((n - n_mean) * (t[i] - t_mean) for n, i in zip(ns, keep)) / sxx
        t0 = t_mean - period * n_mean
        res = {i: t[i] - t0 - round((t[i] - t0) / period) * period for i in range(len(t))}
        med = statistics.median(res[i] for i in keep)
        mad = statistics.median(abs(res[i] - med) for i in keep) * 1.4826
        limit = max(5 * mad, 20e-6)
        keep = [i for i in range(len(t)) if abs(res[i] - med) <= limit]
        if len(keep) < 4:
            keep = list(range(len(t)))
            break
    kept = set(keep)
    residuals = [res[i] if i in kept else None for i in range(len(t))]
    used = [r for r in residuals if r is not None]
    std = statistics.pstdev(used) if len(used) > 1 else float("nan")
    return PeriodFit(period, c, base, t0, std, len(used), len(t), residuals, t)


def circular_stats(angles_rad: list[float]) -> tuple[float, float, float]:
    """Returns (mean in [0, 2pi), resultant length R, circular std)."""
    if not angles_rad:
        return float("nan"), 0.0, float("nan")
    cs = statistics.fmean(math.cos(a) for a in angles_rad)
    sn = statistics.fmean(math.sin(a) for a in angles_rad)
    r = math.hypot(cs, sn)
    mean = math.atan2(sn, cs) % TWO_PI
    std = math.sqrt(-2.0 * math.log(r)) if r > 0 else float("inf")
    return mean, r, std


def wrap_pi(a: float) -> float:
    return (a + math.pi) % TWO_PI - math.pi


def robust_circular_mean(angles_rad: list[float]) -> dict:
    mean, _, _ = circular_stats(angles_rad)
    if not angles_rad:
        return {"mean_deg": None, "std_deg": None, "sem_deg": None, "used": 0, "total": 0}
    devs = [wrap_pi(a - mean) for a in angles_rad]
    mad = statistics.median(abs(d) for d in devs) * 1.4826
    limit = max(5 * mad, math.radians(1.0))
    kept = [a for a, d in zip(angles_rad, devs) if abs(d) <= limit]
    mean, _, std = circular_stats(kept)
    return {"mean_deg": round(math.degrees(mean), 3), "std_deg": round(math.degrees(std), 3),
            "sem_deg": round(math.degrees(std) / math.sqrt(len(kept)), 3),
            "used": len(kept), "total": len(angles_rad)}


def phase_offsets(fit: PeriodFit, target_times_ns: list[int]) -> list[float]:
    """Spindle phase of each target completion relative to the reference sector.

    The reference sector's own residual phase is interpolated at each target time and
    subtracted, which cancels slow drift of the spindle speed over the run.
    """
    ref = [(fit.times_s[i], r) for i, r in enumerate(fit.residuals_s) if r is not None]
    out = []
    for tn in target_times_ns:
        t = (tn - fit.base_ns) * 1e-9
        frac = ((t - fit.t0_s) / fit.period_s) % 1.0
        j = 0
        while j < len(ref) and ref[j][0] < t:
            j += 1
        if j == 0:
            ref_res = ref[0][1]
        elif j == len(ref):
            ref_res = ref[-1][1]
        else:
            (ta, ra), (tb, rb) = ref[j - 1], ref[j]
            ref_res = ra + (rb - ra) * (t - ta) / (tb - ta)
        out.append((TWO_PI * (frac - ref_res / fit.period_s)) % TWO_PI)
    return out
