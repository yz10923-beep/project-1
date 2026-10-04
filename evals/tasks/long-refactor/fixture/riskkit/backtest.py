"""VaR backtesting: exceptions over a rolling window."""

from __future__ import annotations

from riskkit.legacy import legacy_var


def var_series(returns: list[float], window: int = 60) -> list[float]:
    """Rolling 95% VaR (positive loss) from the previous `window` days."""
    return [legacy_var(returns[i - window : i]) for i in range(window, len(returns))]


def exceptions(returns: list[float], window: int = 60, conf: float = 0.99) -> int:
    """Days whose loss exceeded the VaR estimated from the window before them."""
    count = 0
    for i in range(window, len(returns)):
        if -returns[i] > legacy_var(returns[i - window : i], conf):
            count += 1
    return count


def back_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def back_clean(returns: list[float | None]) -> list[float]:
    """Drop missing observations and anything that is not a finite number."""
    out: list[float] = []
    for r in returns:
        if r is None:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue
        out.append(float(r))
    return out


def back_table(rows: list[tuple[str, float]], width: int = 24) -> str:
    """A two-column text table, names left, values right."""
    lines = []
    for name, value in rows:
        lines.append(f"{name:<{width}}{value:>12.6f}")
    return "\n".join(lines)


def back_validate(conf: float) -> float:
    """Reject confidence levels outside (0.5, 1)."""
    if not 0.5 < conf < 1:
        raise ValueError(f"confidence level must be in (0.5, 1), got {conf}")
    return conf


def back_drawdown(returns: list[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path."""
    peak, level, worst = 1.0, 1.0, 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst


def back_summary(returns: list[float]) -> dict[str, float]:
    """Count, mean, min and max of a return series."""
    if not returns:
        return {"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(returns)),
        "mean": sum(returns) / len(returns),
        "min": min(returns),
        "max": max(returns),
    }


def back_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"


def back_stdev(xs: list[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than two points."""
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def back_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Sum two per-name maps; names in either appear in the result."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out
