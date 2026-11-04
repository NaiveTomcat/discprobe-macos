"""Small dense linear algebra, so the calibration needs nothing outside the stdlib.

Everything here is 2x2 or 3x3: the ladder fits three unknowns (u, v, c) against a few
dozen measured phases, and the covariance quadratics are the same size. Plain Python
floats are IEEE doubles, so the accuracy is the same as a BLAS call at this size; the
only thing that matters is pivoting (written below) and column scaling before forming
the normal equations (done by `lstsq`).
"""

from __future__ import annotations


def mat_vec(m: list[list[float]], x: list[float]) -> list[float]:
    return [sum(a * b for a, b in zip(row, x)) for row in m]


def quad(g: list[float], m: list[list[float]]) -> float:
    """g^T M g, the scalar all the uncertainty reports boil down to."""
    return sum(gi * mij * gj for gi, row in zip(g, m) for mij, gj in zip(row, g))


def gram(rows: list[list[float]]) -> list[list[float]]:
    """A^T A for a list of row vectors."""
    n = len(rows[0]) if rows else 0
    out = [[0.0] * n for _ in range(n)]
    for row in rows:
        for i in range(n):
            ri = row[i]
            if ri == 0.0:
                continue
            for j in range(n):
                out[i][j] += ri * row[j]
    return out


def inverse(a: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse with partial pivoting; None if the matrix is singular."""
    n = len(a)
    m = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-300:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        scale = 1.0 / m[col][col]
        m[col] = [v * scale for v in m[col]]
        for r in range(n):
            if r == col or m[r][col] == 0.0:
                continue
            factor = m[r][col]
            m[r] = [v - factor * p for v, p in zip(m[r], m[col])]
    return [row[n:] for row in m]


def solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a square system with partial pivoting; None if it is singular."""
    n = len(a)
    m = [list(map(float, row)) + [float(rhs)] for row, rhs in zip(a, b)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-300:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        pivot_row = m[col]
        for r in range(col + 1, n):
            factor = m[r][col] / pivot_row[col]
            if factor == 0.0:
                continue
            m[r] = [v - factor * p for v, p in zip(m[r], pivot_row)]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def lstsq(rows: list[list[float]], rhs: list[float]) -> tuple[list[float], list[float]]:
    """Least squares by normal equations, after scaling every column to unit norm.

    Returns (solution, column scales). The caller divides by the scales to recover the
    step in the original parameter units, which is what keeps a badly scaled Jacobian
    (u is 1e5, v is a few) from squaring into a rank-deficient normal matrix.
    """
    n = len(rows[0]) if rows else 0
    scales = []
    for c in range(n):
        norm = sum(row[c] * row[c] for row in rows) ** 0.5
        scales.append(norm if norm > 0.0 else 1.0)
    scaled = [[row[c] / scales[c] for c in range(n)] for row in rows]
    normal = gram(scaled)
    rhs_s = [sum(row[c] * y for row, y in zip(scaled, rhs)) for c in range(n)]
    step = solve(normal, rhs_s)
    if step is None:
        step = [0.0] * n
    return step, scales
