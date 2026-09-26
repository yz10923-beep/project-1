from pricing.fx import conv_rate


def quote_in(amount: float, ccy: str, target: str) -> float:
    """Convert `amount` of `ccy` into `target`, rounded to cents."""
    return round(amount * conv_rate(ccy, target), 2)
