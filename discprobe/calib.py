"""The spiral geometry model shared by every measurement here.

A DVD byte at channel-bit position n (counted from LBA 0) lies at accumulated track
angle

    Theta(n) = 2 pi (S - u) / v,   S = sqrt(u^2 + v n / pi),   u = r0 / b,   v = p / b,

with r0 the radius of LBA 0, p the track pitch and b the channel bit length. Only the
ratios u and v matter for angles. The local rate is dTheta/dn = 1 / S = b / r.

`geo` fits u and v to measured sector phases; `disexport` re-expresses the fit as a
DiscImageStudio preset. Everything that turns u and v into a burnable image lives in
DiscImageStudio, not here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from . import dvd, linalg

SECTOR_BITS = 38688


def theta(n: float, u: float, v: float) -> float:
    """Accumulated track angle in radians at channel bit n."""
    s = math.sqrt(u * u + v * n / math.pi)
    # 2 pi (s - u) / v, with s - u written as (s^2 - u^2) / (s + u) to keep precision.
    return 2.0 * n / (s + u)


def rate(n: float, u: float, v: float) -> float:
    """dTheta/dn in radians per channel bit."""
    return 1.0 / math.sqrt(u * u + v * n / math.pi)


@dataclass
class CalLayout:
    s_range: float = 0.012
    s_step: float = 0.002
    groups: int = 3
    start_mm: float = 25.0
    end_mm: float = 57.0
    guard_mm: float = 0.2
    r0_mm: float = 24.0
    pitch_um: float = 0.74
    bit_nm: float = 133.3
    ks: tuple = (3, 9)
    phases_deg: tuple = (0.0, 90.0)
    weights: tuple = (0.3, 0.2)
    guard: str = "92"
    seed: int = 1
    bands: list = field(default_factory=list)
    guards_lba: list = field(default_factory=list)
    total_sectors: int = 0
    kind: str = "dvd-multi-hypothesis-calibration"
    version: int = 1

    @property
    def u0(self) -> float:
        return self.r0_mm / (self.bit_nm * 1e-6)

    @property
    def v0(self) -> float:
        return self.pitch_um * 1e-3 / (self.bit_nm * 1e-6)

    def hypotheses(self) -> list[float]:
        m = int(round(self.s_range / self.s_step))
        return [round(i * self.s_step, 6) for i in range(-m, m + 1)]

    def sector_at(self, r_mm: float) -> int:
        b = self.bit_nm * 1e-6
        n_bits = math.pi * ((r_mm / b) ** 2 - self.u0 ** 2) / self.v0
        return int(round(n_bits / SECTOR_BITS / dvd.BLOCK_SECTORS)) * dvd.BLOCK_SECTORS

    def build(self) -> "CalLayout":
        hyp = self.hypotheses()
        slots = self.groups * len(hyp)
        width = (self.end_mm - self.start_mm - self.guard_mm) / slots
        if width - self.guard_mm < 0.3:
            raise ValueError(f"bands would be {width - self.guard_mm:.2f} mm wide; use fewer hypotheses or groups")
        self.bands, self.guards_lba = [], []
        for g in range(self.groups):
            order = hyp if g % 2 == 0 else hyp[::-1]
            for j, s in enumerate(order):
                i = g * len(hyp) + j
                a = self.start_mm + i * width
                self.guards_lba.append([self.sector_at(a), self.sector_at(a + self.guard_mm)])
                self.bands.append({"index": i, "group": g, "s": s,
                                   "r_start_mm": round(a + self.guard_mm, 4), "r_end_mm": round(a + width, 4),
                                   "lba_start": self.sector_at(a + self.guard_mm),
                                   "lba_end": self.sector_at(a + width)})
        end = self.start_mm + slots * width
        self.guards_lba.append([self.sector_at(end), self.sector_at(end + self.guard_mm)])
        self.total_sectors = self.guards_lba[-1][1]
        return self

    def edge_lbas(self) -> list[int]:
        """Every guard/band boundary, inner to outer."""
        out = []
        for g, b in zip(self.guards_lba, self.bands):
            out += [g[0], b["lba_start"]]
        return out + list(self.guards_lba[-1])

    def pattern(self, thetas) -> list[float]:
        """Spoke pattern value (0..1) at each accumulated track angle, in radians."""
        out = []
        for th in thetas:
            f = 0.5
            for k, ph, w in zip(self.ks, self.phases_deg, self.weights):
                f += w * math.cos(k * th + math.radians(ph))
            out.append(f)
        return out

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "CalLayout":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        for key in ("ks", "phases_deg", "weights"):
            d[key] = tuple(d[key])
        return cls(**d)


def _dtheta(n: float, u: float, v: float) -> tuple[float, float]:
    """Partial derivatives of theta(n) with respect to u and v."""
    s = math.sqrt(u * u + v * n / math.pi)
    return -2 * n / (s * (s + u)), -n * n / (math.pi * s * (s + u) ** 2)


def _report(u, v, cov, coarse, layout: CalLayout, n_of_r) -> dict:
    b_nm = layout.bit_nm
    out = {"u": u, "v": v, "coarse_u": coarse[0], "coarse_v": coarse[1],
           "r0_mm_at_bit": u * b_nm * 1e-6, "pitch_um_at_bit": v * b_nm * 1e-3,
           "bit_nm_assumed": b_nm,
           "note": "angles depend only on u and v; any bit length with r0 = u*b and pitch = v*b gives the same image"}
    b_mm = b_nm * 1e-6
    out["dis_equivalent"] = {
        "bit_nm": b_nm, "inner_radius_mm": u * b_mm,
        "outer_radius_mm_for_total_sectors": {
            str(n): math.sqrt((u * b_mm) ** 2 + v * b_mm * b_mm * n * SECTOR_BITS / math.pi)
            for n in (2295104, 2298496)},
        "note": "DiscImageStudio builds its spiral from inner radius, outer radius and track length "
                "(total sectors x 38688 x bit); use the outer radius for your disc's sector count",
    }
    lo, hi = 24.0, 58.0
    n_lo = math.pi * ((lo / (b_nm * 1e-6)) ** 2 - u * u) / v
    n_hi = math.pi * ((hi / (b_nm * 1e-6)) ** 2 - u * u) / v
    n_lo = max(n_lo, 0.0)
    out["angle_span_mm"] = [lo, hi]
    diff = [math.degrees(theta(n_lo + (n_hi - n_lo) * i / 399.0, u, v)
                         - theta(n_lo + (n_hi - n_lo) * i / 399.0, layout.u0, layout.v0))
            for i in range(400)]
    out["nominal_error_deg"] = max(diff) - min(diff)
    if cov is not None:
        g_hi = _dtheta(n_hi, u, v)
        g_lo = _dtheta(n_lo, u, v)
        g = [g_hi[0] - g_lo[0], g_hi[1] - g_lo[1]]
        sub = [row[:2] for row in cov[:2]]
        out["angle_sigma_deg"] = math.degrees(math.sqrt(max(linalg.quad(g, sub), 0.0)))
        out["u_sigma"] = math.sqrt(cov[0][0])
        out["v_sigma"] = math.sqrt(cov[1][1])
    return out

CHUNK_SECTORS = 2048


