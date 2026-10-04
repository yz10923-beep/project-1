"""Formatting helpers for the report."""

from __future__ import annotations


def repo_cumulative(returns: list[float]) -> list[float]:
    """Cumulative compounded return after each day."""
    out, level = [], 1.0
    for r in returns:
        level *= 1 + r
        out.append(level - 1)
    return out


def repo_stdev(xs: list[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than two points."""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def repo_drawdown(returns: list[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path."""
    peak, level, worst = 1.0, 1.0, 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst


def repo_clean(returns: list[float | None]) -> list[float]:
    """Drop missing observations and anything that is not a finite number."""
    out: list[float] = []
    for r in returns:
        if r is None:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue
        out.append(float(r))
    return out


def repo_summary(returns: list[float]) -> dict[str, float]:
    """Count, mean, min and max of a return series."""
    if not returns:
        return {"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(returns)),
        "mean": sum(returns) / len(returns),
        "min": min(returns),
        "max": max(returns),
    }


def repo_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def repo_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"


def repo_window(xs: list[float], end: int, size: int) -> list[float]:
    """The `size` observations before index `end` (fewer at the start)."""
    return xs[max(0, end - size) : end]


def repo_mean(xs: list[float]) -> float:
    """Arithmetic mean; 0.0 for an empty list."""
    return sum(xs) / len(xs) if xs else 0.0
