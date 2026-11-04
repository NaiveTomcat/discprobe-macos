"""`info`: dump everything the drive reports about itself and the loaded disc."""

from __future__ import annotations

from . import mmc
from .scsi import DATA_IN, DATA_NONE, ScsiResult


def _run(dev, name: str, cdb: bytes, alloc: int, parser=None) -> dict:
    entry = {"cdb": cdb.hex(" ")}
    try:
        res: ScsiResult = dev.execute(cdb, alloc, DATA_IN if alloc else DATA_NONE)
    except OSError as e:
        entry["error"] = str(e)
        return entry
    entry["status"] = res.status
    entry["duration_us"] = round(res.duration_us, 1)
    if res.sense:
        entry["sense"] = str(res.sense)
    if res.data:
        entry["raw"] = res.data.hex()
    if res.ok and parser and res.data:
        try:
            entry["parsed"] = parser(res.data)
        except Exception as e:  # keep the raw dump even if a parser trips
            entry["parse_error"] = repr(e)
    return entry


def collect(dev) -> dict:
    r: dict[str, dict] = {}
    r["test_unit_ready"] = _run(dev, "TUR", mmc.test_unit_ready(), 0)
    r["inquiry"] = _run(dev, "INQUIRY", mmc.inquiry(), 96, mmc.parse_inquiry)
    r["configuration"] = _run(dev, "GET CONFIGURATION", mmc.get_configuration(), 4096,
                              mmc.parse_configuration)
    r["disc_information"] = _run(dev, "READ DISC INFORMATION", mmc.read_disc_information(), 34,
                                 mmc.parse_disc_information)
    r["track_1"] = _run(dev, "READ TRACK INFORMATION", mmc.read_track_information(1), 48,
                        mmc.parse_track_information)
    r["capacity"] = _run(dev, "READ CAPACITY", mmc.read_capacity(), 8, mmc.parse_capacity)
    r["toc"] = _run(dev, "READ TOC", mmc.read_toc(0), 2048, mmc.parse_toc)
    r["atip"] = _run(dev, "READ ATIP", mmc.read_toc(4, msf=True), 2048, mmc.parse_atip)
    r["dvd_physical"] = _run(dev, "DVD physical format", mmc.read_disc_structure(0, 0x00), 2052,
                             mmc.parse_dvd_physical)
    # 10h: for DVD-R, the physical format information recorded in the lead-in
    # (00h may come from the latest border-in instead).
    r["dvd_physical_leadin_10"] = _run(dev, "DVD lead-in physical format", mmc.read_disc_structure(0, 0x10),
                                       2052, mmc.parse_dvd_physical)
    r["dvd_prerecorded_0e"] = _run(dev, "DVD-R pre-recorded info", mmc.read_disc_structure(0, 0x0E),
                                   2052, mmc.parse_dvd_r_prerecorded)
    r["dvd_plus_adip_11"] = _run(dev, "DVD+R ADIP", mmc.read_disc_structure(0, 0x11), 2052,
                                 mmc.parse_dvd_plus_adip)
    r["bd_disc_information"] = _run(dev, "BD disc information", mmc.read_disc_structure(1, 0x00),
                                    4100, lambda d: {"ascii_runs": mmc.ascii_runs(d[4:])})
    r["perf_read_nominal"] = _run(dev, "GET PERFORMANCE read", mmc.get_performance(0x10, 0x00),
                                  8 + 16 * 16, lambda d: mmc.parse_get_performance(d, 0x00))
    r["perf_write_nominal"] = _run(dev, "GET PERFORMANCE write", mmc.get_performance(0x14, 0x00),
                                   8 + 16 * 16, lambda d: mmc.parse_get_performance(d, 0x00))
    r["perf_write_speeds"] = _run(dev, "GET PERFORMANCE write speeds", mmc.get_performance(0, 0x03),
                                  8 + 16 * 16, lambda d: mmc.parse_get_performance(d, 0x03))
    r["mode_page_2a"] = _run(dev, "MODE SENSE 2A", mmc.mode_sense10(0x2A), 256, mmc.parse_mode_page_2a)
    return r


def summarize(r: dict) -> list[str]:
    def parsed(key):
        return r.get(key, {}).get("parsed") or {}

    lines = []
    inq = parsed("inquiry")
    if inq:
        lines.append(f"Drive: {inq['vendor']} {inq['product']} rev {inq['revision']}")
    cfg = parsed("configuration")
    if cfg:
        lines.append(f"Current profile: {cfg.get('current_profile_name')} "
                     f"(0x{cfg.get('current_profile') or 0:04X})")
    di = parsed("disc_information")
    if di:
        lines.append(f"Disc status: {di['disc_status']}, sessions {di['sessions']}, erasable {di['erasable']}")
    cap = parsed("capacity")
    if cap.get("last_lba") is not None:
        lines.append(f"Readable capacity: last LBA {cap['last_lba']} x {cap['block_length']} B")
    atip = parsed("atip")
    if atip:
        lines.append(f"ATIP: {atip['disc_type']}, lead-in start {atip['leadin_start_msf']} "
                     f"(manufacturer code), last lead-out start {atip['last_leadout_start_msf']} "
                     f"= LBA {atip['last_leadout_start_lba']}")
    phys = parsed("dvd_physical")
    if phys:
        # DVD+R written with book-type bitsetting may report DVD-ROM here; trust the profile.
        lines.append(f"DVD physical: {phys['book_type']} {phys['disc_size']}, {phys['layers']} layer(s), "
                     f"{phys['linear_density']}, {phys['track_density']} (nominal codes), "
                     f"PSN {phys['data_start_psn']}..{phys['data_end_psn']}")
    pre = parsed("dvd_prerecorded_0e")
    if pre.get("manufacturer_id"):
        lines.append(f"DVD-R MID: {pre['manufacturer_id']!r}")
    elif pre.get("ascii_runs"):
        lines.append(f"DVD-R pre-recorded text: {' | '.join(pre['ascii_runs'])}")
    adip = parsed("dvd_plus_adip_11")
    if adip.get("manufacturer_id"):
        lines.append(f"DVD+R MID: {adip['manufacturer_id']}/{adip['media_type_id']}/{adip['product_revision']}")
    for key, label in (("perf_read_nominal", "Read perf"), ("perf_write_nominal", "Write perf")):
        for desc in parsed(key).get("descriptors", []):
            lines.append(f"{label}: LBA {desc['start_lba']}..{desc['end_lba']}  "
                         f"{desc['start_kBps']} -> {desc['end_kBps']} kB/s "
                         f"(ratio {desc.get('end_over_start')})")
    for desc in parsed("perf_write_speeds").get("descriptors", []):
        lines.append(f"Write speed descriptor: read {desc['read_kBps']} / write {desc['write_kBps']} kB/s "
                     f"to LBA {desc['end_lba']}")
    page = parsed("mode_page_2a")
    if page:
        lines.append(f"Page 2A: buffer {page['buffer_kB']} kB, rotation {page['rotation_control_selected']}, "
                     f"write speeds {[(w['kBps'], w['rotation']) for w in page['write_speeds']]}")
    failed = [k for k, v in r.items() if v.get("status") not in (0, None) or "error" in v]
    if failed:
        lines.append("Not available on this drive/disc: " + ", ".join(
            f"{k} ({r[k].get('sense') or r[k].get('error')})" for k in failed))
    return lines
