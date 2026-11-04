"""MMC command builders and response parsers (read-only subset).

Field offsets follow MMC-6. Parsers never raise on short responses; anything they
cannot interpret is left for the raw hex dump that the info command always keeps.
"""

from __future__ import annotations

import re


def _be(data: bytes, start: int, length: int) -> int | None:
    if len(data) < start + length:
        return None
    return int.from_bytes(data[start:start + length], "big")


def _u16(v: int) -> list[int]:
    return [(v >> 8) & 0xFF, v & 0xFF]


def _u32(v: int) -> list[int]:
    return [(v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF]


# ---------------------------------------------------------------- builders

def test_unit_ready() -> bytes:
    return bytes(6)


def inquiry(alloc: int = 96) -> bytes:
    return bytes([0x12, 0, 0, 0, alloc, 0])


def get_configuration(rt: int = 0, start_feature: int = 0, alloc: int = 4096) -> bytes:
    return bytes([0x46, rt & 3, *_u16(start_feature), 0, 0, 0, *_u16(alloc), 0])


def read_disc_information(alloc: int = 34) -> bytes:
    return bytes([0x51, 0, 0, 0, 0, 0, 0, *_u16(alloc), 0])


def read_track_information(track: int, alloc: int = 48) -> bytes:
    # Address/Number Type 01b: logical track number.
    return bytes([0x52, 0x01, *_u32(track), 0, *_u16(alloc), 0])


def read_capacity() -> bytes:
    return bytes([0x25] + [0] * 9)


def read_toc(fmt: int, msf: bool = False, track: int = 0, alloc: int = 2048) -> bytes:
    return bytes([0x43, 0x02 if msf else 0, fmt & 0x0F, 0, 0, 0, track, *_u16(alloc), 0])


def read_disc_structure(media_type: int, fmt: int, address: int = 0, layer: int = 0,
                        alloc: int = 4100) -> bytes:
    return bytes([0xAD, media_type & 0x0F, *_u32(address), layer, fmt, *_u16(alloc), 0, 0])


def get_performance(data_type: int, perf_type: int, start_lba: int = 0, max_desc: int = 16) -> bytes:
    return bytes([0xAC, data_type & 0x1F, *_u32(start_lba), 0, 0, *_u16(max_desc), perf_type, 0])


def mode_sense10(page: int, alloc: int = 256) -> bytes:
    # DBD=1: no block descriptors, so the page follows the 8-byte header.
    return bytes([0x5A, 0x08, page & 0x3F, 0, 0, 0, 0, *_u16(alloc), 0])


def read10(lba: int, count: int = 1, fua: bool = False) -> bytes:
    return bytes([0x28, 0x08 if fua else 0, *_u32(lba), 0, *_u16(count), 0])


def read12(lba: int, count: int = 1, fua: bool = False) -> bytes:
    return bytes([0xA8, 0x08 if fua else 0, *_u32(lba), *_u32(count), 0, 0])


def read_cd(lba: int, count: int = 1, sector_type: int = 1) -> bytes:
    # Expected sector type 1 = CD-DA; byte 9 = 0x10 returns user data only.
    return bytes([0xBE, (sector_type & 7) << 2, *_u32(lba),
                  (count >> 16) & 0xFF, (count >> 8) & 0xFF, count & 0xFF, 0x10, 0, 0])


def set_cd_speed(read_kbs: int = 0xFFFF, write_kbs: int = 0xFFFF, pure_cav: bool = False) -> bytes:
    return bytes([0xBB, 1 if pure_cav else 0, *_u16(read_kbs), *_u16(write_kbs), 0, 0, 0, 0, 0, 0])


# ---------------------------------------------------------------- tables

PROFILES = {
    0x0008: "CD-ROM", 0x0009: "CD-R", 0x000A: "CD-RW",
    0x0010: "DVD-ROM", 0x0011: "DVD-R Sequential", 0x0012: "DVD-RAM",
    0x0013: "DVD-RW Restricted Overwrite", 0x0014: "DVD-RW Sequential",
    0x0015: "DVD-R DL Sequential", 0x0016: "DVD-R DL Jump", 0x001A: "DVD+RW",
    0x001B: "DVD+R", 0x002A: "DVD+RW DL", 0x002B: "DVD+R DL",
    0x0040: "BD-ROM", 0x0041: "BD-R SRM", 0x0042: "BD-R RRM", 0x0043: "BD-RE",
}

DVD_BOOK_TYPES = {0: "DVD-ROM", 1: "DVD-RAM", 2: "DVD-R", 3: "DVD-RW", 9: "DVD+RW",
                  10: "DVD+R", 13: "DVD+RW DL", 14: "DVD+R DL"}

# Nominal category codes from the physical format information, not measurements.
DVD_LINEAR_DENSITY = {0: "0.267 um/bit", 1: "0.293 um/bit", 2: "0.409-0.435 um/bit",
                      4: "0.280-0.291 um/bit", 8: "0.353 um/bit"}
DVD_TRACK_DENSITY = {0: "0.74 um/track", 1: "0.80 um/track", 2: "0.615 um/track"}

DISC_STATUS = {0: "empty", 1: "incomplete", 2: "complete", 3: "other"}


def media_family(profile: int | None) -> str:
    if profile is None:
        return "unknown"
    if 0x08 <= profile <= 0x0A:
        return "cd"
    if 0x10 <= profile <= 0x2B:
        return "dvd"
    if 0x40 <= profile <= 0x43:
        return "bd"
    return "unknown"


def block_sectors(family: str) -> int:
    """Sectors per error-correction block: the smallest unit a drive reads from media."""
    return {"dvd": 16, "bd": 32}.get(family, 1)


def msf_to_lba(m: int, s: int, f: int) -> int:
    frames = (m * 60 + s) * 75 + f
    return frames - 450150 if m >= 90 else frames - 150


def ascii_runs(data: bytes, min_len: int = 3) -> list[str]:
    return [m.decode("ascii") for m in re.findall(rb"[\x20-\x7E]{%d,}" % min_len, data)]


# ---------------------------------------------------------------- parsers

def parse_inquiry(d: bytes) -> dict:
    def text(a, b):
        return d[a:b].decode("ascii", "replace").strip()
    return {"vendor": text(8, 16), "product": text(16, 32), "revision": text(32, 36),
            "vendor_specific": text(36, 56)}


def parse_configuration(d: bytes) -> dict:
    out = {"current_profile": _be(d, 6, 2), "profiles": [], "features": []}
    cur = out["current_profile"]
    out["current_profile_name"] = PROFILES.get(cur, "unknown") if cur is not None else None
    end = min(len(d), (_be(d, 0, 4) or 0) + 4)
    pos = 8
    while pos + 4 <= end:
        code = _be(d, pos, 2)
        flags = d[pos + 2]
        length = d[pos + 3]
        body = d[pos + 4:pos + 4 + length]
        out["features"].append({"code": f"0x{code:04X}", "current": bool(flags & 1),
                                "version": (flags >> 2) & 0x0F, "data": body.hex()})
        if code == 0x0000:
            for i in range(0, len(body) - 3, 4):
                p = _be(body, i, 2)
                out["profiles"].append({"profile": f"0x{p:04X}", "name": PROFILES.get(p, "unknown"),
                                        "current": bool(body[i + 2] & 1)})
        pos += 4 + length
    return out


def parse_disc_information(d: bytes) -> dict:
    if len(d) < 24:
        return {}
    return {
        "erasable": bool(d[2] & 0x10),
        "last_session_state": (d[2] >> 2) & 3,
        "disc_status": DISC_STATUS[d[2] & 3],
        "first_track": d[3],
        "sessions": d[4] | (d[9] << 8),
        "first_track_last_session": d[5] | (d[10] << 8),
        "last_track_last_session": d[6] | (d[11] << 8),
        "disc_type": d[8],
        "last_session_leadin_msf": list(d[17:20]),
        "last_possible_leadout_msf": list(d[21:24]),
        "last_possible_leadout_lba_if_msf": msf_to_lba(d[21], d[22], d[23]),
    }


def parse_track_information(d: bytes) -> dict:
    if len(d) < 32:
        return {}
    return {
        "track": d[2] | (d[32] << 8 if len(d) > 32 else 0),
        "session": d[3],
        "track_mode": d[5] & 0x0F,
        "blank": bool(d[6] & 0x40),
        "data_mode": d[6] & 0x0F,
        "nwa_valid": bool(d[7] & 1),
        "lra_valid": bool(d[7] & 2),
        "track_start": _be(d, 8, 4),
        "next_writable": _be(d, 12, 4),
        "free_blocks": _be(d, 16, 4),
        "track_size": _be(d, 24, 4),
        "last_recorded": _be(d, 28, 4),
    }


def parse_capacity(d: bytes) -> dict:
    return {"last_lba": _be(d, 0, 4), "block_length": _be(d, 4, 4)}


def parse_toc(d: bytes) -> dict:
    tracks = []
    for pos in range(4, len(d) - 7, 8):
        control = d[pos + 1] & 0x0F
        tracks.append({"track": d[pos + 2], "data": bool(control & 0x04),
                       "control": control, "start_lba": int.from_bytes(d[pos + 4:pos + 8], "big", signed=True)})
    return {"first": d[2] if len(d) > 2 else None, "last": d[3] if len(d) > 3 else None, "tracks": tracks}


def parse_atip(d: bytes) -> dict:
    if len(d) < 4 + 11:
        return {}
    a = d[4:]
    leadin = (a[4], a[5], a[6])
    leadout = (a[8], a[9], a[10])
    return {
        "writing_power": (a[0] >> 4) & 7,
        "reference_speed": a[0] & 7,
        "unrestricted_use": bool(a[1] & 0x40),
        "disc_type": "CD-RW" if a[2] & 0x40 else "CD-R",
        "disc_subtype": (a[2] >> 3) & 7,
        # The lead-in start time identifies the dye/manufacturer (Orange Book table).
        "leadin_start_msf": "%02d:%02d:%02d" % leadin,
        "leadin_start_lba": msf_to_lba(*leadin),
        # Last possible lead-out start on classic media; newer formats may put SAL here.
        "last_leadout_start_msf": "%02d:%02d:%02d" % leadout,
        "last_leadout_start_lba": msf_to_lba(*leadout),
        "a1_a2_a3_valid": [bool(a[2] & 4), bool(a[2] & 2), bool(a[2] & 1)],
    }


def parse_dvd_physical(d: bytes) -> dict:
    if len(d) < 4 + 17:
        return {}
    p = d[4:]
    book = p[0] >> 4
    return {
        "book_type": DVD_BOOK_TYPES.get(book, f"code {book}"),
        "part_version": p[0] & 0x0F,
        "disc_size": "120 mm" if p[1] >> 4 == 0 else "80 mm" if p[1] >> 4 == 1 else f"code {p[1] >> 4}",
        "max_rate_code": p[1] & 0x0F,
        "layers": ((p[2] >> 5) & 3) + 1,
        "track_path": "OTP" if p[2] & 0x10 else "PTP",
        "layer_type_bits": p[2] & 0x0F,
        "linear_density": DVD_LINEAR_DENSITY.get(p[3] >> 4, f"code {p[3] >> 4}"),
        "track_density": DVD_TRACK_DENSITY.get(p[3] & 0x0F, f"code {p[3] & 0x0F}"),
        "data_start_psn": f"0x{_be(p, 5, 3):06X}",
        "data_end_psn": f"0x{_be(p, 9, 3):06X}",
        "layer0_end_psn": f"0x{_be(p, 13, 3):06X}",
        "bca_present": bool(p[16] & 0x80),
    }


def parse_dvd_r_prerecorded(d: bytes) -> dict:
    """DVD-R pre-recorded information (format 0Eh). Offsets are into the full response
    including its 4-byte header, as in MMC-6 and dvd+rw-mediainfo: the MID is two
    non-adjacent 6-byte fields after Field IDs 3 and 4."""
    out = {"ascii_runs": ascii_runs(d[4:])}
    if len(d) >= 35 and d[4] == 1:
        out["last_recordable_address"] = _be(d, 7, 3)
    if len(d) >= 35 and d[20] == 3 and d[28] == 4:
        out["manufacturer_id"] = (d[21:27] + d[29:35]).decode("ascii", "replace")
        out["manufacturer_id_hex"] = (d[21:27] + d[29:35]).hex()
    return out


def parse_dvd_plus_adip(d: bytes) -> dict:
    # Best effort: DVD+R/+RW ADIP keeps the manufacturer ID at bytes 19..26,
    # media type ID at 27..29 and product revision at 30 of the 2048-byte block.
    if len(d) < 4 + 31:
        return {}
    p = d[4:]
    return {"manufacturer_id": p[19:27].decode("ascii", "replace").strip("\x00 "),
            "media_type_id": p[27:30].decode("ascii", "replace").strip("\x00 "),
            "product_revision": p[30], "ascii_runs": ascii_runs(p)}


def parse_get_performance(d: bytes, perf_type: int) -> dict:
    if len(d) < 8:
        return {}
    length = min(len(d), (_be(d, 0, 4) or 0) + 4)
    out = {"write": bool(d[4] & 2), "except": bool(d[4] & 1), "descriptors": []}
    for pos in range(8, length - 15, 16):
        if perf_type == 0x00:
            out["descriptors"].append({
                "start_lba": _be(d, pos, 4), "start_kBps": _be(d, pos + 4, 4),
                "end_lba": _be(d, pos + 8, 4), "end_kBps": _be(d, pos + 12, 4)})
        elif perf_type == 0x03:
            out["descriptors"].append({
                "wrc": (d[pos] >> 3) & 3, "rdd": bool(d[pos] & 4), "exact": bool(d[pos] & 2),
                "end_lba": _be(d, pos + 4, 4), "read_kBps": _be(d, pos + 8, 4),
                "write_kBps": _be(d, pos + 12, 4)})
    for desc in out["descriptors"]:
        if perf_type == 0x00 and desc["start_kBps"]:
            # Under pure CAV this ratio equals outer/inner radius of the range.
            desc["end_over_start"] = round(desc["end_kBps"] / desc["start_kBps"], 4)
    return out


def parse_mode_page_2a(d: bytes) -> dict:
    if len(d) < 8:
        return {}
    bdl = _be(d, 6, 2) or 0
    p = d[8 + bdl:]
    if len(p) < 2 or (p[0] & 0x3F) != 0x2A:
        return {}
    out = {
        "read_caps": f"0x{p[2]:02X}", "write_caps": f"0x{p[3]:02X}",
        "buffer_kB": _be(p, 12, 2),
        "max_read_kBps_obsolete": _be(p, 8, 2),
        "current_read_kBps_obsolete": _be(p, 14, 2),
        "max_write_kBps_obsolete": _be(p, 18, 2),
        "rotation_control_selected": None, "current_write_kBps": None, "write_speeds": [],
    }
    if len(p) >= 32:
        out["rotation_control_selected"] = "CAV" if p[27] & 3 == 1 else "CLV" if p[27] & 3 == 0 else f"code {p[27] & 3}"
        out["current_write_kBps"] = _be(p, 28, 2)
        count = _be(p, 30, 2) or 0
        for i in range(count):
            pos = 32 + 4 * i
            if pos + 4 > len(p):
                break
            out["write_speeds"].append({"rotation": "CAV" if p[pos + 1] & 3 == 1 else "CLV",
                                        "kBps": _be(p, pos + 2, 2)})
    return out
