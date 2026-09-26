"""FX conversion rates for the pricing service."""

CONV_RATE_TTL = 30  # seconds a cached rate stays valid

_RATES = {("EUR", "USD"): 1.0850, ("GBP", "USD"): 1.2710, ("USD", "JPY"): 151.20}
fx_rate_cache: dict[tuple[str, str], float] = {}


def fx_rate(base: str, quote: str) -> float:
    """Units of `quote` per one unit of `base`."""
    if base == quote:
        return 1.0
    key = (base, quote)
    if key not in fx_rate_cache:
        if key in _RATES:
            fx_rate_cache[key] = _RATES[key]
        elif (quote, base) in _RATES:
            fx_rate_cache[key] = 1 / _RATES[(quote, base)]
        else:
            raise KeyError(f"no rate for {base}/{quote}")
    return fx_rate_cache[key]


def inv_fx_rate(base: str, quote: str) -> float:
    """Units of `base` per one unit of `quote`."""
    return 1 / fx_rate(base, quote)
