def historical_var(returns: list[float], confidence: float) -> float:
    """One-day loss not exceeded with `confidence` probability, as a positive fraction."""
    ordered = sorted(returns)
    return -ordered[int((1 - confidence) * len(ordered))]
