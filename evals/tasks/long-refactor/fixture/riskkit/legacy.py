"""Deprecated risk measures, kept for old callers. Use riskkit.measures instead.

Both functions return a LOSS as a positive number (a 2% loss is 0.02).
"""


def legacy_var(returns: list[float], conf: float = 0.95) -> float:
    """Historical value-at-risk at `conf`, as a positive loss."""
    xs = sorted(returns)
    k = int((1 - conf) * len(xs))
    return -xs[k]


def legacy_es(returns: list[float], conf: float = 0.95) -> float:
    """Historical expected shortfall at `conf`: the mean loss beyond VaR, positive."""
    xs = sorted(returns)
    k = max(1, int((1 - conf) * len(xs)))
    return -sum(xs[:k]) / k
