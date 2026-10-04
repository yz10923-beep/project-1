"""Limit checks against VaR and ES."""

from __future__ import annotations

from riskkit.measures import ExpectedShortfall, VaR


def breaches_var_limit(returns: list[float], limit: float, conf: float = 0.975) -> bool:
    """True when VaR (a positive loss) is above the limit."""
    return -VaR(conf).of(returns) > limit


def es_headroom(returns: list[float], limit: float) -> float:
    """How much ES (95%, positive loss) is left under the limit."""
    return limit + ExpectedShortfall(0.95).of(returns)


def limi_validate(conf: float) -> float:
    """Reject confidence levels outside (0.5, 1)."""
    if not 0.5 < conf < 1:
        raise ValueError(f"confidence level must be in (0.5, 1), got {conf}")
    return conf


def limi_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def limi_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"


def limi_summary(returns: list[float]) -> dict[str, float]:
    """Count, mean, min and max of a return series."""
    if not returns:
        return {"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(returns)),
        "mean": sum(returns) / len(returns),
        "min": min(returns),
        "max": max(returns),
    }


def limi_clean(returns: list[float | None]) -> list[float]:
    """Drop missing observations and anything that is not a finite number."""
    out: list[float] = []
    for r in returns:
        if r is None:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue
        out.append(float(r))
    return out


def limi_window(xs: list[float], end: int, size: int) -> list[float]:
    """The `size` observations before index `end` (fewer at the start)."""
    return xs[max(0, end - size) : end]


def limi_cumulative(returns: list[float]) -> list[float]:
    """Cumulative compounded return after each day."""
    out, level = [], 1.0
    for r in returns:
        level *= 1 + r
        out.append(level - 1)
    return out


def limi_drawdown(returns: list[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path."""
    peak, level, worst = 1.0, 1.0, 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst


def limi_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Sum two per-name maps; names in either appear in the result."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out
