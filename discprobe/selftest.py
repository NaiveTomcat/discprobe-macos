"""Self-test: parser checks on hand-built responses, then the full timing run on the
simulated drive, compared against the simulator's true geometry."""

from __future__ import annotations

import math

from . import analysis, mmc, timing
from .scsi import check_allowed
from .sim import SimDrive

PHASE_SEM_FACTOR = 4.5


class Checker:
    def __init__(self):
        self.failures = 0

    def check(self, cond: bool, what: str):
        print(("  ok    " if cond else "  FAIL  ") + what)
        if not cond:
            self.failures += 1


def _parsers(c: Checker):
    print("parsers")
    atip = bytes([0, 26, 0, 0, 0x55, 0x40, 0x80, 0, 97, 26, 66, 0, 79, 59, 74, 0] + [0] * 14)
    a = mmc.parse_atip(atip)
    c.check(a["leadin_start_msf"] == "97:26:66" and a["leadin_start_lba"] == -11634, "ATIP lead-in time")
    c.check(a["last_leadout_start_lba"] == 359849, "ATIP lead-out = CD80 capacity")
    c.check(mmc.msf_to_lba(0, 2, 0) == 0, "MSF 00:02:00 is LBA 0")

    perf = bytes([0, 0, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x2B, 0x48,
                  0, 0x23, 0x05, 0x3F, 0, 0, 0x56, 0x90])
    d = mmc.parse_get_performance(perf, 0x00)["descriptors"][0]
    c.check(d["start_kBps"] == 11080 and d["end_kBps"] == 22160 and d["end_over_start"] == 2.0,
            "GET PERFORMANCE descriptor")

    phys = bytearray(4 + 2048)
    phys[4:8] = bytes([0x25, 0x0F, 0x00, 0x00])
    phys[9:12] = (0x030000).to_bytes(3, "big")
    phys[13:16] = (0x26053F).to_bytes(3, "big")
    p = mmc.parse_dvd_physical(bytes(phys))
    c.check(p["book_type"] == "DVD-R" and p["track_density"] == "0.74 um/track"
            and p["data_start_psn"] == "0x030000" and p["data_end_psn"] == "0x26053F", "DVD physical format")

    pre = bytearray(4 + 40)
    pre[4] = 1
    pre[20], pre[21:27] = 3, b"MCC 03"
    pre[28], pre[29:35] = 4, b"RG20\x00\x00"
    c.check(mmc.parse_dvd_r_prerecorded(bytes(pre)).get("manufacturer_id") == "MCC 03RG20\x00\x00",
            "DVD-R MID from Field IDs 3 and 4")

    for op, name in ((0x2A, "WRITE(10)"), (0xAA, "WRITE(12)"), (0x5B, "CLOSE TRACK"), (0x04, "FORMAT UNIT")):
        try:
            check_allowed(bytes([op] + [0] * 9))
            c.check(False, f"{name} refused")
        except PermissionError:
            c.check(True, f"{name} refused")


def _circ_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _sim_run(c: Checker, mode: str, fua: bool, expect_strategy: str, expect_rotation: str,
             flush: bool = False, sectors: int = 1, reread_ms: float = 0.0):
    print(f"simulated drive: {mode}, FUA {'honoured' if fua else 'ignored'}, "
          f"zero-length flush {'honoured' if flush else 'ignored'}, {sectors}-sector reads"
          + (f", {reread_ms} ms firmware overhead per media read" if reread_ms else ""))
    sim = SimDrive(mode=mode, fua_honoured=fua, flush_honoured=flush, seed=7, reread_delay_ms=reread_ms)
    probe = timing.TimingProbe(sim, log=lambda *_: None, sectors=sectors)
    s = probe.run()
    c.check(s["cache"]["strategy"] == expect_strategy, f"cache strategy {s['cache']['strategy']}")
    rot = s["period"]["rotation"]
    c.check(rot["mode"] == expect_rotation,
            f"rotation {rot['mode']} (period ratio {rot.get('period_ratio_outer_inner')})")
    if mode == "cav":
        for z in s["period"]["zones"]:
            c.check(abs(z["period_ms"] - 6.6) < 0.01, f"zone {z['fraction']}: T {z['period_ms']} ms")
        worst = 0.0
        for t in s["phase"]["targets"]:
            truth = sim.true_phase_deg(t["ref_lba"], t["target_lba"])
            err = _circ_diff(t["phase"]["mean_deg"], truth)
            worst = max(worst, err)
            # Across seeds the errors are unbiased and spread about 1.1-1.2x the reported
            # sem, so the bound scales with it.
            c.check(err < max(PHASE_SEM_FACTOR * t["phase"]["sem_deg"], 1.0),
                    f"target {t['target_fraction']}: measured {t['phase']['mean_deg']:.2f}, "
                    f"true {truth:.2f}, error {err:.2f} deg (sem {t['phase']['sem_deg']})")
        print(f"  worst phase error {worst:.2f} deg")
    return s


def _harmonics(c: Checker):
    print("period estimation")
    period = 0.0071
    import random
    rng = random.Random(3)
    t, times = 0.0, []
    for _ in range(50):
        t += period * rng.randint(3, 17)
        times.append(int((t + rng.gauss(0, 50e-6)) * 1e9))
    fit = analysis.estimate_period(times)
    c.check(fit is not None and abs(fit.period_s - period) < 2e-6,
            f"fundamental found among harmonics: {fit.period_s * 1e3:.5f} ms")
    c.check(fit is not None and 30 < fit.residual_std_s * 1e6 < 80, f"jitter {fit.residual_std_s * 1e6:.1f} us")


def _ladder(c: Checker):
    print("ladder geometry measurement on the simulated drive (35 ms re-read overhead)")
    from .calib import SECTOR_BITS, theta
    from .geo import Ladder
    sim = SimDrive(mode="cav", seed=7, reread_delay_ms=35.0)
    lad = Ladder(sim, speed_kbs=None, reps=16, max_targets=14, log=lambda *_: None)
    res = lad.run()
    b = 133.6e-6  # the simulator's off-nominal geometry
    n_top = 0.85 * sim.capacity * SECTOR_BITS
    e = [math.degrees(theta(n_top * i / 399.0, lad.u, lad.v)
                      - theta(n_top * i / 399.0, 23.93 / b, 0.735e-3 / b)) for i in range(400)]
    span = max(e) - min(e)
    c.check(span < 1.5, f"{len(lad.targets)} rungs, angle error over 0-85 % of the disc {span:.3f} deg "
                        f"(reported 1 sigma {res['angle_sigma_measured_span_deg']:.3f}, "
                        f"chi2/dof {res['chi2_per_dof']:.2f})")


def _ladder_abort(c: Checker):
    print("ladder stops on a wrong whole-turn count")
    from .geo import Ladder

    class OffByOneTurn(Ladder):
        def _add(self, m, predicted_deg):
            if len(self.targets) == 5:      # sixth rung: pretend the prediction picked the wrong turn
                predicted_deg += 360.0
            super()._add(m, predicted_deg)

    sim = SimDrive(mode="cav", seed=7, reread_delay_ms=35.0)
    lad = OffByOneTurn(sim, speed_kbs=None, reps=16, max_targets=10, log=lambda *_: None)
    try:
        lad.run()
        c.check(False, "a one-turn error was not detected")
    except RuntimeError as e:
        c.check("whole-turn" in str(e), f"stopped after {len(lad.targets)} rungs: {str(e)[:60]}...")


def _disexport(c: Checker):
    print("DiscImageStudio preset export")
    from .disexport import fit_preset
    u, v = 24.06 / 133.3e-6, 0.7453e-3 / 133.3e-6
    f = fit_preset(u, v, 133.3, 2298496, 25.8, 54.4)
    c.check(f["max_angle_diff_deg"] < 0.005 and max(abs(f["pitchLinear"]), abs(f["pitchQuadratic"]), abs(f["pitchCubic"])) < 1e-7
            and abs(f["innerRadiusMm"] - 24.06) < 1e-5,
            f"constant-pitch map exports with ~zero cubic terms, max angle difference {f['max_angle_diff_deg']:.4f} deg")


def run() -> bool:
    c = Checker()
    _parsers(c)
    _harmonics(c)
    _sim_run(c, "cav", True, "fua", "CAV")
    _sim_run(c, "cav", True, "fua", "CAV", sectors=16)
    _sim_run(c, "cav", True, "fua", "CAV", reread_ms=35.0)
    _sim_run(c, "cav", False, "flush", "CAV", flush=True)
    _sim_run(c, "cav", False, "evict", "CAV")
    _sim_run(c, "clv", True, "fua", "CLV")
    _ladder(c)
    _ladder_abort(c)
    _disexport(c)
    print(f"\n{'all checks passed' if c.failures == 0 else f'{c.failures} check(s) failed'}")
    return c.failures == 0
