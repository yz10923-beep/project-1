# pricing-lib

`fx_rate(base, quote)` returns units of `quote` per one unit of `base`.
Use `inv_fx_rate(base, quote)` for the inverse.

Rates are cached in `fx_rate_cache` for `CONV_RATE_TTL` seconds.
