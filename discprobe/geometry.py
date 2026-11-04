"""Constant-pitch spiral: sector index -> radius and accumulated track angle.

Used only for nominal context (expected CLV period ratio, turns between sectors)
and by the simulated drive. Uses the area approximation r^2 = r0^2 + p*L/pi,
which is within ~1e-4 degrees of the exact arc-length spiral over a whole disc.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DVD_SECTOR_MM = 38688 * 133.3e-6
CD_SECTOR_MM = 1200.0 / 75.0          # 1.2 m/s, 75 sectors per second
BD_SECTOR_MM = 962136 * 74.5e-6 / 32  # one recording unit carries 32 sectors


@dataclass(frozen=True)
class Spiral:
    r0_mm: float
    pitch_mm: float
    sector_mm: float

    def radius(self, sector: float) -> float:
        return math.sqrt(self.r0_mm ** 2 + self.pitch_mm * sector * self.sector_mm / math.pi)

    def angle(self, sector: float) -> float:
        """Accumulated track angle in radians since sector 0."""
        return 2.0 * math.pi * (self.radius(sector) - self.r0_mm) / self.pitch_mm

    def turns(self, a: float, b: float) -> float:
        return (self.angle(b) - self.angle(a)) / (2.0 * math.pi)


NOMINAL = {
    "dvd": Spiral(24.0, 0.74e-3, DVD_SECTOR_MM),
    "cd": Spiral(25.0, 1.5e-3, CD_SECTOR_MM),
    "bd": Spiral(24.0, 0.32e-3, BD_SECTOR_MM),
}

# Linear velocity at 1x in mm/s, for converting a drive speed to revolution periods.
ONE_X_MM_S = {"dvd": 26.16e6 * 133.3e-6, "cd": 1200.0, "bd": 66e6 * 74.5e-6}
