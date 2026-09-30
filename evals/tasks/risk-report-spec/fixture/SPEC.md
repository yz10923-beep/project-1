# riskreport: spec

A small end-of-day risk report over a trade blotter. `riskreport/model.py` (the
`Trade` dataclass) is done; everything else is a stub. Only the standard library is
available at runtime (pytest is available for tests).

## Loading

- **R1.** `riskreport.io.load_trades(path)` reads a CSV with the header
  `ts,symbol,side,qty,price` and returns `(trades, rejected)`: a list of `Trade`
  sorted by `ts` (ascending), and the number of rows that were skipped. `ts` is an
  ISO-8601 timestamp parsed into a `datetime`, `qty` an `int`, `price` a `float`.
  Symbols are stripped and upper-cased; `side` is matched case-insensitively and
  stored as `"BUY"` or `"SELL"`.
- **R2.** A row with `qty <= 0`, a side other than buy/sell, or a value that does not
  parse is skipped and counted in `rejected`. It never raises.
- **R3.** `riskreport.io.load_prices(path)` reads a CSV with the header `symbol,price`
  and returns `dict[str, float]`, with symbols normalised as in R1.

## Metrics (`riskreport/metrics.py`)

- **R4.** `positions(trades) -> dict[str, int]`: net quantity per symbol (buys minus
  sells). Symbols whose net quantity is 0 are left out.
- **R5.** `avg_cost(trades, symbol) -> float`: quantity-weighted average price of the
  symbol's BUY trades only; `0.0` if it has none.
- **R6.** `realized_pnl(trades, symbol) -> float`: realized profit using **FIFO** lots.
  Each SELL is matched against the oldest remaining BUY quantity first; P&L is
  `(sell price - lot price) * matched quantity`, summed. Inputs never sell more than
  is held.
- **R7.** `exposure(positions, prices) -> dict[str, float]`: market value
  (`qty * price`) per symbol. A symbol with no price raises `KeyError` whose message
  contains the symbol.

## Command line

- **R8.** `python -m riskreport TRADES_CSV PRICES_CSV` prints exactly one JSON object
  to stdout:

  ```json
  {"positions": {"KO": 100},
   "gross_exposure": 6280.0,
   "realized_pnl": 53.5,
   "top": ["KO"],
   "rejected_rows": 1}
  ```

  `gross_exposure` is the sum of absolute market values, `realized_pnl` the sum of
  R6 over all symbols, `top` the symbols with the largest absolute market value
  (descending, ties by symbol name), at most 3. Floats are rounded to 2 decimals.
- **R9.** `--top N` sets how many symbols `top` holds. If a position has no price,
  the command prints an error naming the symbol to stderr and exits with code 2
  (no traceback).

## Tests

- **R10.** Add `tests/test_metrics.py` with at least three tests covering
  `positions`, `avg_cost` and `realized_pnl`. `python -m pytest -q` passes. Do not
  change `tests/test_model.py`, this spec or anything in `data/`.
