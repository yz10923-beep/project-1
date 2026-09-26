from pricing import fx


def usd_exposure(positions: dict[str, float]) -> float:
    """Sum of positions, each converted to USD."""
    return round(sum(amt * fx.fx_rate(ccy, "USD") for ccy, amt in positions.items()), 2)
