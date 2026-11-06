"""Command line: python -m discprobe {list,info,timing,geo,disexport,random,selftest}"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sys

from . import info, timing



def _backend():
    """The SCSI pass-through back end. This repository ships the macOS one only."""
    if sys.platform != "darwin":
        sys.exit(f"this build has no back end for {sys.platform}; it carries the macOS IOKit one. "
                 f"The Windows SPTI back end lives in Ashley-QwQ/disc-probe.")
    from . import macscsi as module
    return module


def _open(args):
    if args.sim:
        from .sim import SimDrive
        mode, _, variant = args.sim.partition("-")
        return SimDrive(mode=mode, fua_honoured=variant == "", flush_honoured=variant == "flush",
                        seed=args.seed)
    if not args.drive:
        hint = "disk10"
        sys.exit(f"give --drive {hint} (see `python -m discprobe list`) "
                 f"or --sim cav|clv|cav-nofua|cav-flush")
    return _backend().open_device(args.drive)


def _outdir(args, kind: str, dev) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = getattr(dev, "name", "drive").replace(":", "")
    path = os.path.join(args.out, f"{kind}-{tag}-{stamp}")
    os.makedirs(path, exist_ok=True)
    return path


def _write_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def cmd_list(args):
    drives = _backend().list_optical_drives()
    if not drives:
        print("no optical drives found")
        return
    for drive in drives:
        if isinstance(drive, str):
            print(drive)
        else:
            model = " ".join(x for x in (drive.get("vendor"), drive.get("product"),
                                         drive.get("revision")) if x)
            print(f"{drive['name']:10s} {model}".rstrip())


def cmd_info(args):
    dev = _open(args)
    try:
        report = info.collect(dev)
    finally:
        dev.close() if hasattr(dev, "close") else None
    out = _outdir(args, "info", dev)
    _write_json(os.path.join(out, "info.json"), report)
    for line in info.summarize(report):
        print(line)
    print(f"\nFull dump: {os.path.abspath(out)}")


def cmd_timing(args):
    if not args.sim:
        if not _backend().raise_priority():
            print("note: could not raise process priority")
    dev = _open(args)
    targets = [float(x) for x in args.targets.split(",")] if args.targets else timing.DEFAULT_TARGETS
    probe = timing.TimingProbe(
        dev, read_method=args.read, reps=args.reps, period_reads=args.period_reads,
        targets=targets, ref_fraction=args.ref, speed_kbs=args.speed, pure_cav=args.cav,
        evict_sectors=args.evict_sectors, sectors=args.sectors, host_delay_ms=args.host_delay_ms)
    try:
        summary = probe.run()
    except timing.NoReadableDisc as e:
        sys.exit(str(e))
    finally:
        dev.close() if hasattr(dev, "close") else None
        out = _outdir(args, "timing", dev)
        rows = probe.events_as_rows()
        if rows:
            with open(os.path.join(out, "events.csv"), "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
        summary = probe.summary
        summary["device"] = getattr(dev, "name", None)
        summary["args"] = {k: v for k, v in vars(args).items() if k != "func"}
        _write_json(os.path.join(out, "summary.json"), summary)
        print(f"\nRaw events and summary: {os.path.abspath(out)}")








def cmd_geo(args):
    from .geo import Ladder, write_outputs
    ranges = None
    if args.ranges:
        ranges = [tuple(int(x) for x in part.split("-")) for part in args.ranges.split(",")]
    if not args.sim:
        _backend().raise_priority()
    dev = _open(args)
    speed = args.speed if args.speed is not None else "default"
    ladder = Ladder(dev, ranges=ranges, r0_mm=args.r0_mm, pitch_um=args.pitch_um, bit_nm=args.bit_nm,
                    reps=args.reps, max_targets=args.max_targets, ref_lba=args.ref_lba,
                    speed_kbs=None if args.sim else speed, host_delay_ms=args.host_delay_ms)
    out = _outdir(args, "geo", dev)
    try:
        res = ladder.run()
    except timing.NoReadableDisc as e:
        sys.exit(f"stopped: {e}")
    except RuntimeError as e:
        print(f"stopped: {e}")
        res = None
    finally:
        dev.close() if hasattr(dev, "close") else None
        if ladder.targets and ladder.cov is not None:
            write_outputs(ladder, out)
    if res:
        print()
        print(f"u = r0/b = {res['u']:.3f}, v = p/b = {res['v']:.7f}  (chi2/dof {res['chi2_per_dof']:.2f}, "
              f"residual rms {res['residual_rms_deg']:.3f} deg)")
        print(f"With bit length {res['bit_nm_assumed']} nm: r0 {res['r0_mm_at_bit']:.5f} mm, "
              f"pitch {res['pitch_um_at_bit']:.6f} um")
        print(f"Angle uncertainty across the measured span {res['measured_span_mm'][0]}-"
              f"{res['measured_span_mm'][1]} mm: {res['angle_sigma_measured_span_deg']:.3f} deg (1 sigma)"
              + (f"; across 24-58 mm: {res['angle_sigma_deg']:.2f} deg" if "angle_sigma_deg" in res else ""))
    print(f"targets.csv, summary.json, events.csv: {os.path.abspath(out)}")





def cmd_disexport(args):
    from .disexport import export
    try:
        out = export(args.geo, args.sectors, args.output, args.r_min, args.r_max, args.id, args.name)
    except ValueError as e:
        sys.exit(str(e))
    f = out["fit"]
    print(f"inner {f['innerRadiusMm']:.9f} mm, outer {f['outerRadiusMm']:.9f} mm, pitch shape "
          f"{f['pitchLinear']:.3e} {f['pitchQuadratic']:.3e} {f['pitchCubic']:.3e}")
    print(f"angle difference to the measured map over {f['fit_range_mm'][0]}-{f['fit_range_mm'][1]} mm: "
          f"max {f['max_angle_diff_deg']:.4f} deg, rms {f['rms_angle_diff_deg']:.4f} deg")
    print(f"DiscImageStudio preset object in {os.path.abspath(args.output)} (key 'preset')")




def cmd_random(args):
    from .randomdisc import write_iso
    print(f"Writing {os.path.abspath(args.output)}: {args.sectors} sectors of seeded random data")
    write_iso(args.output, args.sectors, args.seed)
    print("Burn it with any image burner (Windows: Disc Image Burner; macOS: hdiutil burn), then run `geo`.")


def cmd_selftest(args):
    from .selftest import run
    sys.exit(0 if run() else 1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="discprobe", description="Read-only optical drive probe")
    sub = p.add_subparsers(required=True)

    sp = sub.add_parser("list", help="list optical drives")
    sp.set_defaults(func=cmd_list)

    def common(sp):
        sp.add_argument("--drive", help="drive letter on Windows (e.g. E:) or BSD name on macOS "
                                        "(e.g. disk10, or just 10)")
        sp.add_argument("--sim", choices=["cav", "clv", "cav-nofua", "cav-flush"],
                        help="use the simulated drive (nofua: FUA ignored; flush: FUA ignored but a "
                             "zero-length FUA read drops the cache)")
        sp.add_argument("--seed", type=int, default=1, help="simulation seed")
        sp.add_argument("--out", default="results", help="output directory (default: results)")

    sp = sub.add_parser("info", help="dump drive and disc information")
    common(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("timing", help="cache/FUA test, CAV check, sector phase measurement")
    common(sp)
    sp.add_argument("--read", choices=["auto", "read10", "read12", "readcd"], default="auto")
    sp.add_argument("--reps", type=int, default=40, help="R/X pairs per phase target")
    sp.add_argument("--period-reads", type=int, default=60, help="reads per zone in the period test")
    sp.add_argument("--targets", help="comma-separated disc fractions, default 0.15,0.35,0.55,0.75,0.95")
    sp.add_argument("--ref", type=float, default=0.05, help="reference sector as a disc fraction")
    sp.add_argument("--speed", type=int,
                    help="SET CD SPEED read speed in kB/s, e.g. DVD 8x = 11080, 16x = 22160 (omit to leave as is)")
    sp.add_argument("--cav", action="store_true",
                    help="set the Rotational Control bit of SET CD SPEED; MMC defines it for writing, "
                         "so whether reads change is only known from the period test")
    sp.add_argument("--sectors", type=int, default=1,
                    help="sectors per measurement read; 16 reads one whole aligned DVD ECC block")
    sp.add_argument("--host-delay-ms", type=float, default=25.0,
                    help="random pause (0..N ms) before each timed read, so repeated reads do not all "
                         "span the same number of revolutions; should exceed one revolution")
    sp.add_argument("--evict-sectors", type=int, default=1024,
                    help="sectors read elsewhere before each measurement when the cache cannot be bypassed")
    sp.set_defaults(func=cmd_timing)

    sp = sub.add_parser("geo", help="measure the spiral geometry of a DVD or Mode 1 CD from read timing")
    common(sp)
    sp.add_argument("--speed", type=int, metavar="KBPS",
                    help="read speed to fix (default: DVD 5540 kB/s, CD 1411 kB/s)")
    sp.add_argument("--ranges", help="ladder LBA ranges a-b,c-d, end exclusive (default 3-85 %% of the recorded area)")
    sp.add_argument("--ref-lba", type=int)
    sp.add_argument("--reps", type=int, help="reference/target read pairs per rung (DVD 24, CD 32)")
    sp.add_argument("--max-targets", type=int, help="maximum rungs (DVD 22, CD 36)")
    sp.add_argument("--host-delay-ms", type=float, help="maximum random host delay (DVD 25, CD 50 ms)")
    sp.add_argument("--r0-mm", type=float, help="prior radius (DVD 24, CD 25 mm)")
    sp.add_argument("--pitch-um", type=float, help="prior pitch (DVD 0.74, CD 1.6 um)")
    sp.add_argument("--bit-nm", type=float, help="assumed channel bit length (DVD 133.3, CD 277.66 nm)")
    sp.set_defaults(func=cmd_geo)

    sp = sub.add_parser("disexport", help="fit the measured angle map as a DiscImageStudio DVD preset (cubic pitch)")
    sp.add_argument("--geo", required=True, help="summary.json of a geo run")
    sp.add_argument("--sectors", type=int, default=2298496, help="totalSectors of the preset (disc capacity)")
    sp.add_argument("--r-min", type=float, default=25.8, help="fit range: use the measured span")
    sp.add_argument("--r-max", type=float, default=54.4)
    sp.add_argument("--id", default="discprobe-dvd-r")
    sp.add_argument("--name", default="DVD-R (drive-timing calibration)")
    sp.add_argument("--output", default="dis_preset.json")
    sp.set_defaults(func=cmd_disexport)

    sp = sub.add_parser("random", help="write a random-data ISO for full-disc timing calibration")
    sp.add_argument("--output", default="iso/random.iso")
    sp.add_argument("--sectors", type=int, default=2295104)
    sp.add_argument("--seed", type=int, default=1)
    sp.set_defaults(func=cmd_random)

    sp = sub.add_parser("selftest", help="parser checks and end-to-end runs on the simulated drive")
    sp.set_defaults(func=cmd_selftest)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
