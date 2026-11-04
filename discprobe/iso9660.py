"""Minimal empty ISO 9660 file system, so burning tools accept a raw pattern image.

Windows' Disc Image Burner rejects an image without a recognisable file system. This
writes a primary volume descriptor (LBA 16), a terminator (17), the two path tables
(18, 19) and an empty root directory (20). Those five sectors lie in the filler
before the first ring or band (inside 25 mm), so the pattern is untouched. Layout per
ECMA-119; numbers are both-endian where the standard says so.
"""

from __future__ import annotations

import datetime
import struct

SECTORS = range(16, 21)
_PVD, _TERM, _LPATH, _MPATH, _ROOT = SECTORS


def _both32(v: int) -> bytes:
    return struct.pack("<I", v) + struct.pack(">I", v)


def _both16(v: int) -> bytes:
    return struct.pack("<H", v) + struct.pack(">H", v)


def _dir_record(name: bytes, when: datetime.datetime) -> bytes:
    rec = bytearray(33 + len(name) + (1 if len(name) % 2 == 0 else 0))
    rec[0] = len(rec)
    rec[2:10] = _both32(_ROOT)
    rec[10:18] = _both32(2048)
    rec[18:25] = bytes([when.year - 1900, when.month, when.day, when.hour, when.minute, when.second, 0])
    rec[25] = 0x02  # directory
    rec[28:32] = _both16(1)
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def _text(s: str, n: int) -> bytes:
    return s.encode("ascii")[:n].ljust(n, b" ")


def _date17(when: datetime.datetime | None) -> bytes:
    if when is None:
        return b"0" * 16 + b"\x00"
    return when.strftime("%Y%m%d%H%M%S").encode() + b"00\x00"


def sectors(total_sectors: int, volume_id: str) -> dict[int, bytes]:
    when = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    pvd = bytearray(2048)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[8:40] = _text("", 32)
    pvd[40:72] = _text(volume_id.upper(), 32)
    pvd[80:88] = _both32(total_sectors)
    pvd[120:124] = _both16(1)
    pvd[124:128] = _both16(1)
    pvd[128:132] = _both16(2048)
    pvd[132:140] = _both32(10)
    pvd[140:144] = struct.pack("<I", _LPATH)
    pvd[148:152] = struct.pack(">I", _MPATH)
    pvd[156:190] = _dir_record(b"\x00", when)
    pvd[190:813] = b" " * (813 - 190)
    pvd[813:830] = _date17(when)
    pvd[830:847] = _date17(when)
    pvd[847:864] = _date17(None)
    pvd[864:881] = _date17(None)
    pvd[881] = 1

    term = bytearray(2048)
    term[0] = 255
    term[1:6] = b"CD001"
    term[6] = 1

    lpath = bytearray(2048)
    lpath[0:10] = bytes([1, 0]) + struct.pack("<I", _ROOT) + struct.pack("<H", 1) + b"\x00\x00"
    mpath = bytearray(2048)
    mpath[0:10] = bytes([1, 0]) + struct.pack(">I", _ROOT) + struct.pack(">H", 1) + b"\x00\x00"

    root = bytearray(2048)
    dot, dotdot = _dir_record(b"\x00", when), _dir_record(b"\x01", when)
    root[0:len(dot)] = dot
    root[len(dot):len(dot) + len(dotdot)] = dotdot
    return {_PVD: bytes(pvd), _TERM: bytes(term), _LPATH: bytes(lpath), _MPATH: bytes(mpath), _ROOT: bytes(root)}


def add(path: str, total_sectors: int, volume_id: str) -> None:
    with open(path, "r+b") as f:
        for lba, data in sectors(total_sectors, volume_id).items():
            f.seek(lba * 2048)
            f.write(data)
