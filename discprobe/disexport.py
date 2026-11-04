"""Export a measured angle map as a DiscImageStudio DVD preset (cubic track pitch).

DiscImageStudio models a DVD by its inner and outer radius, the sector count, a fixed
channel bit length, and a pitch p(x) = p0 (1 + c1 x + c2 x^2 + c3 x^3), x = (r - ri) /
(ro - ri), with p0 set by the total track length. That is a different family from our
constant-pitch u, v model, so the conversion is a fit: ri, ro, c1, c2, c3 are chosen to
reproduce our angle as a function of track position over the measured radius range (an
overall rotation is free), and the remaining angle difference is reported so the export
can be judged before use.

Thin-track approximation for arc length (ds = 2 pi r / p dr); against the exact arc
length this changes angles by well under 0.001 degree over a whole DVD.

Standard library only, like the rest of the measurement path: the five-parameter fit is
a small damped Gauss-Newton written out below.
"""

from __future__ import annotations

import math

from .calib import SECTOR_BITS, theta

GRID = 40001        # radius samples in the angle map
SAMPLES = 600       # track positions the residual is evaluated at


def _cumtrapz(values: list[float], step: float) -> list[float]:
    out = [0.0] * len(values)
    acc = 0.0
    prev = values[0]
    for i in range(1, len(values)):
        cur = values[i]
        acc += (cur + prev) * 0.5 * step
        prev = cur
        out[i] = acc
    return out


def dis_angle_map(ri, ro, c1, c2, c3, total_sectors, bit_nm):
    """Track position n (channel bits) and accumulated angle (rad) on a radius grid."""
    b = bit_nm * 1e-6
    span = ro - ri
    if span <= 0.0:
        return None
    step = span / (GRID - 1)
    radii = [ri + step * i for i in range(GRID)]
    shape = []
    for r in radii:
        x = (r - ri) / span
        s = 1.0 + c1 * x + c2 * x * x + c3 * x * x * x
        if s <= 0.0:
            return None
        shape.append(s)
    s_unit = _cumtrapz([2.0 * math.pi * r / s for r, s in zip(radii, shape)], step)
    ang = _cumtrapz([2.0 * math.pi / s for s in shape], step)
    length = total_sectors * SECTOR_BITS * b
    p0 = s_unit[-1] / length
    return [v / p0 / b for v in s_unit], [v / p0 for v in ang], p0


def _interp(xs: list[float], xp: list[float], fp: list[float]) -> list[float]:
    """Linear interpolation of (xp, fp) at the ascending points xs, in one pass."""
    out = []
    j, last = 0, len(xp) - 1
    for x in xs:
        if x <= xp[0]:
            out.append(fp[0])
            continue
        if x >= xp[last]:
            out.append(fp[last])
            continue
        while j + 1 < last and xp[j + 1] < x:
            j += 1
        t = (x - xp[j]) / (xp[j + 1] - xp[j])
        out.append(fp[j] + t * (fp[j + 1] - fp[j]))
    return out


def _least_squares(fun, x0, max_iter: int = 60, xtol: float = 1e-14, ftol: float = 1e-14,
                   gtol: float = 1e-14, step: float = 1e-6) -> list[float]:
    """Levenberg-Marquardt on a small residual vector.

    Five unknowns against 600 sample angles with a smooth residual, so a damped
    Gauss-Newton with a central-difference Jacobian reaches machine precision in a few
    dozen iterations. Damping is raised on a rejected step and lowered on an accepted
    one, which is what keeps the badly scaled parameters (two radii in mm, three
    relative pitch coefficients) from mattering.
    """
    x = [float(v) for v in x0]
    r = list(fun(x))
    cost = 0.5 * sum(v * v for v in r)
    damping = 1e-3
    n = len(x)
    for _ in range(max_iter):
        cols = []
        for i in range(n):
            h = step * max(abs(x[i]), 1.0)
            up = list(x)
            up[i] += h
            down = list(x)
            down[i] -= h
            fu = fun(up)
            fd = fun(down)
            cols.append([(a - b) / (2.0 * h) for a, b in zip(fu, fd)])
        # normal matrix and gradient of a column-scaled model, same reason as linalg.lstsq
        scale = [max(sum(c * c for c in col) ** 0.5, 1e-300) for col in cols]
        cols = [[c / s for c in col] for col, s in zip(cols, scale)]
        normal = [[sum(a * b for a, b in zip(ci, cj)) for cj in cols] for ci in cols]
        grad = [sum(a * b for a, b in zip(ci, r)) for ci in cols]
        if max(abs(g) for g in grad) <= gtol:
            break
        damped = [[normal[i][j] + (damping * normal[i][i] if i == j else 0.0) for j in range(n)]
                  for i in range(n)]
        step_scaled = _solve(damped, [-g for g in grad])
        if step_scaled is None:
            break
        trial = [x[i] + step_scaled[i] / scale[i] for i in range(n)]
        r_trial = list(fun(trial))
        cost_trial = 0.5 * sum(v * v for v in r_trial)
        if cost_trial < cost:
            delta = [trial[i] - x[i] for i in range(n)]
            converged = (abs(cost - cost_trial) <= ftol * max(cost, 1e-30) or
                         math.sqrt(sum(d * d for d in delta)) <= xtol * (math.sqrt(sum(v * v for v in x)) + xtol))
            x, r, cost = trial, r_trial, cost_trial
            damping = max(damping * 0.3, 1e-12)
            if converged:
                break
        else:
            damping *= 10.0
            if damping > 1e9:
                break
    return x


def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for a tiny square system."""
    n = len(a)
    m = [list(row) + [rhs] for row, rhs in zip(a, b)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-300:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        pivot_row = m[col]
        for r in range(col + 1, n):
            factor = m[r][col] / pivot_row[col]
            if factor:
                m[r] = [v - factor * p for v, p in zip(m[r], pivot_row)]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit_preset(u, v, bit_nm, total_sectors, r_lo, r_hi):
    b = bit_nm * 1e-6
    n_lo = math.pi * ((r_lo / b) ** 2 - u * u) / v
    n_hi = math.pi * ((r_hi / b) ** 2 - u * u) / v
    n_eval = [n_lo + (n_hi - n_lo) * i / (SAMPLES - 1) for i in range(SAMPLES)]
    want = [theta(n, u, v) for n in n_eval]
    ri0 = u * b
    ro0 = b * math.sqrt(u * u + v * total_sectors * SECTOR_BITS / math.pi)
    scale = [1e-3, 1e-3, 1e-4, 1e-4, 1e-4]   # mm, mm, relative

    def resid(z):
        ri = ri0 + z[0] * scale[0]
        ro = ro0 + z[1] * scale[1]
        c1 = z[2] * scale[2]
        c2 = z[3] * scale[3]
        c3 = z[4] * scale[4]
        m = dis_angle_map(ri, ro, c1, c2, c3, total_sectors, bit_nm)
        if m is None:
            return [1e3] * len(n_eval)
        n, th, _ = m
        d = [a - b for a, b in zip(_interp(n_eval, n, th), want)]
        mean = sum(d) / len(d)
        return [math.degrees(x - mean) for x in d]

    z = _least_squares(resid, [0.0] * 5)
    ri = ri0 + z[0] * scale[0]
    ro = ro0 + z[1] * scale[1]
    c1, c2, c3 = z[2] * scale[2], z[3] * scale[3], z[4] * scale[4]
    d = resid(z)
    _, _, p0 = dis_angle_map(ri, ro, c1, c2, c3, total_sectors, bit_nm)
    return {"innerRadiusMm": ri, "outerRadiusMm": ro, "pitchLinear": c1,
            "pitchQuadratic": c2, "pitchCubic": c3, "p0_um": p0 * 1e3,
            "fit_range_mm": [r_lo, r_hi], "max_angle_diff_deg": max(abs(x) for x in d),
            "rms_angle_diff_deg": math.sqrt(sum(x * x for x in d) / len(d)),
            "peak_to_peak_angle_diff_deg": max(d) - min(d)}


def preset_json(fit, total_sectors, bit_nm, preset_id, name, description):
    return {"id": preset_id, "displayName": name, "description": description,
            "totalSectors": total_sectors, "innerRadiusMm": fit["innerRadiusMm"],
            "outerRadiusMm": fit["outerRadiusMm"], "channelBitLengthNm": bit_nm,
            "pitchLinear": fit["pitchLinear"], "pitchQuadratic": fit["pitchQuadratic"],
            "pitchCubic": fit["pitchCubic"]}


def export(geo_summary: str, total_sectors: int, out_path: str, r_lo: float, r_hi: float,
           preset_id: str, name: str) -> dict:
    import json
    with open(geo_summary, encoding="utf-8") as f:
        geo = json.load(f)["fit"]
    fit = fit_preset(geo["u"], geo["v"], geo["bit_nm_assumed"], total_sectors, r_lo, r_hi)
    desc = (f"Drive-timing calibration (discprobe); fitted over {r_lo}-{r_hi} mm, "
            f"max angle difference to the measured map {fit['max_angle_diff_deg']:.3f} deg")
    out = {"preset": preset_json(fit, total_sectors, geo["bit_nm_assumed"], preset_id, name, desc),
           "fit": fit,
           "source": {"geo": geo_summary.replace("\\", "/").split("/")[-1]}}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out
