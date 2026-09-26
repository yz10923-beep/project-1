from pricing.fx import fx_rate


def quote_in(amount: float, ccy: str, target: str) -> float:
    """Convert `amount` of `ccy` into `target`, rounded to cents."""
    return round(amount * fx_rate(ccy, target), 2)
