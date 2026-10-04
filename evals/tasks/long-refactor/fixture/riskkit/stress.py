"""Stress scenarios: shocked return series."""

from __future__ import annotations

from riskkit import legacy


def stressed_es(returns: list[float], shock: float) -> float:
    """97.5% ES of the returns scaled by `shock`, as a positive loss."""
    shocked = [r * shock for r in returns]
    return legacy.legacy_es(shocked, 0.975)


def stre_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Sum two per-name maps; names in either appear in the result."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def stre_stdev(xs: list[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than two points."""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def stre_summary(returns: list[float]) -> dict[str, float]:
    """Count, mean, min and max of a return series."""
    if not returns:
        return {"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(returns)),
        "mean": sum(returns) / len(returns),
        "min": min(returns),
        "max": max(returns),
    }


def stre_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def stre_cumulative(returns: list[float]) -> list[float]:
    """Cumulative compounded return after each day."""
    out, level = [], 1.0
    for r in returns:
        level *= 1 + r
        out.append(level - 1)
    return out


def stre_window(xs: list[float], end: int, size: int) -> list[float]:
    """The `size` observations before index `end` (fewer at the start)."""
    return xs[max(0, end - size) : end]


def stre_drawdown(returns: list[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path."""
    peak, level, worst = 1.0, 1.0, 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst


def stre_mean(xs: list[float]) -> float:
    """Arithmetic mean; 0.0 for an empty list."""
    return sum(xs) / len(xs) if xs else 0.0


def stre_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"
