"""Business-day helpers."""

from __future__ import annotations


def cale_window(xs: list[float], end: int, size: int) -> list[float]:
    """The `size` observations before index `end` (fewer at the start)."""
    return xs[max(0, end - size) : end]


def cale_cumulative(returns: list[float]) -> list[float]:
    """Cumulative compounded return after each day."""
    out, level = [], 1.0
    for r in returns:
        level *= 1 + r
        out.append(level - 1)
    return out


def cale_table(rows: list[tuple[str, float]], width: int = 24) -> str:
    """A two-column text table, names left, values right."""
    lines = []
    for name, value in rows:
        lines.append(f"{name:<{width}}{value:>12.6f}")
    return "\n".join(lines)


def cale_mean(xs: list[float]) -> float:
    """Arithmetic mean; 0.0 for an empty list."""
    return sum(xs) / len(xs) if xs else 0.0


def cale_validate(conf: float) -> float:
    """Reject confidence levels outside (0.5, 1)."""
    if not 0.5 < conf < 1:
        raise ValueError(f"confidence level must be in (0.5, 1), got {conf}")
    return conf


def cale_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Sum two per-name maps; names in either appear in the result."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def cale_clean(returns: list[float | None]) -> list[float]:
    """Drop missing observations and anything that is not a finite number."""
    out: list[float] = []
    for r in returns:
        if r is None:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue
        out.append(float(r))
    return out


def cale_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def cale_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"
