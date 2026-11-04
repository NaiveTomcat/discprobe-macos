"""Device-independent SCSI plumbing: results, sense data and the opcode allowlist."""

from __future__ import annotations

from dataclasses import dataclass

DATA_OUT, DATA_IN, DATA_NONE = 0, 1, 2

# Every opcode the probe may send. All of them only read from the drive, except
# SET CD SPEED, which changes the spindle speed and nothing on the disc. Anything
# else is refused before it reaches the device.
ALLOWED_OPCODES = {
    0x00: "TEST UNIT READY",
    0x12: "INQUIRY",
    0x25: "READ CAPACITY",
    0x28: "READ(10)",
    0x43: "READ TOC/PMA/ATIP",
    0x46: "GET CONFIGURATION",
    0x51: "READ DISC INFORMATION",
    0x52: "READ TRACK INFORMATION",
    0x5A: "MODE SENSE(10)",
    0xA8: "READ(12)",
    0xAC: "GET PERFORMANCE",
    0xAD: "READ DISC STRUCTURE",
    0xBB: "SET CD SPEED",
    0xBE: "READ CD",
}


def check_allowed(cdb: bytes) -> str:
    name = ALLOWED_OPCODES.get(cdb[0])
    if name is None:
        raise PermissionError(f"opcode 0x{cdb[0]:02X} is not on the read-only allowlist")
    return name


@dataclass(frozen=True)
class Sense:
    key: int
    asc: int
    ascq: int

    def __str__(self) -> str:
        return f"{self.key:X}/{self.asc:02X}/{self.ascq:02X}"


def parse_sense(buf: bytes) -> Sense | None:
    if len(buf) < 4:
        return None
    code = buf[0] & 0x7F
    if code in (0x70, 0x71) and len(buf) >= 14:
        return Sense(buf[2] & 0x0F, buf[12], buf[13])
    if code in (0x72, 0x73):
        return Sense(buf[1] & 0x0F, buf[2], buf[3])
    return None


@dataclass
class ScsiResult:
    status: int
    data: bytes
    sense: Sense | None
    t_start_ns: int
    t_end_ns: int

    @property
    def ok(self) -> bool:
        return self.status == 0

    @property
    def duration_us(self) -> float:
        return (self.t_end_ns - self.t_start_ns) / 1000.0


class ScsiCommandError(Exception):
    def __init__(self, name: str, result: ScsiResult):
        super().__init__(f"{name} failed: status 0x{result.status:02X}, sense {result.sense}")
        self.name = name
        self.result = result
