"""Per-book risk for a portfolio of books."""

from __future__ import annotations

from riskkit.legacy import legacy_var as lv


def book_vars(books: dict[str, list[float]], conf: float = 0.99) -> dict[str, float]:
    """VaR (positive loss) per book."""
    return {name: lv(rets, conf) for name, rets in books.items()}


def worst_book(books: dict[str, list[float]]) -> str:
    """The book with the largest 99% VaR."""
    vars_ = book_vars(books)
    return max(vars_, key=lambda name: vars_[name])


def port_mean(xs: list[float]) -> float:
    """Arithmetic mean; 0.0 for an empty list."""
    return sum(xs) / len(xs) if xs else 0.0


def port_scale(returns: list[float], target_vol: float) -> list[float]:
    """Rescale returns to a target volatility (no-op for a flat series)."""
    n = len(returns)
    if n < 2:
        return list(returns)
    m = sum(returns) / n
    vol = (sum((r - m) ** 2 for r in returns) / (n - 1)) ** 0.5
    if vol == 0:
        return list(returns)
    return [r * target_vol / vol for r in returns]


def port_table(rows: list[tuple[str, float]], width: int = 24) -> str:
    """A two-column text table, names left, values right."""
    lines = []
    for name, value in rows:
        lines.append(f"{name:<{width}}{value:>12.6f}")
    return "\n".join(lines)


def port_drawdown(returns: list[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative return path."""
    peak, level, worst = 1.0, 1.0, 0.0
    for r in returns:
        level *= 1 + r
        peak = max(peak, level)
        worst = max(worst, 1 - level / peak)
    return worst


def port_summary(returns: list[float]) -> dict[str, float]:
    """Count, mean, min and max of a return series."""
    if not returns:
        return {"n": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {
        "n": float(len(returns)),
        "mean": sum(returns) / len(returns),
        "min": min(returns),
        "max": max(returns),
    }


def port_clean(returns: list[float | None]) -> list[float]:
    """Drop missing observations and anything that is not a finite number."""
    out: list[float] = []
    for r in returns:
        if r is None:
            continue
        if r != r or r in (float("inf"), float("-inf")):
            continue
        out.append(float(r))
    return out


def port_window(xs: list[float], end: int, size: int) -> list[float]:
    """The `size` observations before index `end` (fewer at the start)."""
    return xs[max(0, end - size) : end]


def port_to_bp(x: float) -> str:
    """Format a return as basis points, one decimal."""
    return f"{x * 1e4:.1f}bp"


def port_merge(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Sum two per-name maps; names in either appear in the result."""
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out
