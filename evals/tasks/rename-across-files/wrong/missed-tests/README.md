# pricing-lib

`fx_rate(base, quote)` returns units of `quote` per one unit of `base`.
Use `inv_conv_rate(base, quote)` for the inverse.

Rates are cached in `conv_rate_cache` for `CONV_RATE_TTL` seconds.
