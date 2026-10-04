"""Risk measures (riskkit 2.x).

Sign convention: every measure returns a quantile or mean of P&L RETURNS, so a loss is
NEGATIVE (a 2% loss is -0.02). This is the opposite of riskkit.legacy, whose functions
returned losses as positive numbers. There is no default confidence level: pass it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VaR:
    """Historical value-at-risk: the (1 - conf) quantile of returns."""

    conf: float

    def of(self, returns: list[float]) -> float:
        xs = sorted(returns)
        k = int((1 - self.conf) * len(xs))
        return xs[k]


@dataclass(frozen=True)
class ExpectedShortfall:
    """Historical expected shortfall: the mean of the returns at or below VaR."""

    conf: float

    def of(self, returns: list[float]) -> float:
        xs = sorted(returns)
        k = max(1, int((1 - self.conf) * len(xs)))
        return sum(xs[:k]) / k
