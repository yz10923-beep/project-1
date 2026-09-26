"""Mean-reversion signal on the spread between two tickers."""

import statistics


def zscore(spread: list[float], window: int = 20) -> float:
    recent = spread[-window:]
    return (recent[-1] - statistics.fmean(recent)) / statistics.pstdev(recent)


def signal(spread: list[float], entry: float = 2.0) -> int:
    """-1 short the spread, +1 long it, 0 flat."""
    z = zscore(spread)
    return -1 if z > entry else 1 if z < -entry else 0
