"""Daily FX report. Columns name the pricing function to call, so ops can add a column
without touching this code."""

from pricing import fx

COLUMNS = [
    ("EUR", "USD", "fx_rate"),
    ("GBP", "USD", "fx_rate"),
    ("USD", "JPY", "inv_conv_rate"),
]


def build() -> list[str]:
    rows = []
    for base, quote, fn_name in COLUMNS:
        value = getattr(fx, fn_name)(base, quote)
        rows.append(f"{base}/{quote} {fn_name} {value:.4f}")
    return rows
