"""FX conversion rates for the pricing service."""

CONV_RATE_TTL = 30  # seconds a cached rate stays valid

_RATES = {("EUR", "USD"): 1.0850, ("GBP", "USD"): 1.2710, ("USD", "JPY"): 151.20}
conv_rate_cache: dict[tuple[str, str], float] = {}


def conv_rate(base: str, quote: str) -> float:
    """Units of `quote` per one unit of `base`."""
    if base == quote:
        return 1.0
    key = (base, quote)
    if key not in conv_rate_cache:
        if key in _RATES:
            conv_rate_cache[key] = _RATES[key]
        elif (quote, base) in _RATES:
            conv_rate_cache[key] = 1 / _RATES[(quote, base)]
        else:
            raise KeyError(f"no rate for {base}/{quote}")
    return conv_rate_cache[key]


def inv_conv_rate(base: str, quote: str) -> float:
    """Units of `base` per one unit of `quote`."""
    return 1 / conv_rate(base, quote)
