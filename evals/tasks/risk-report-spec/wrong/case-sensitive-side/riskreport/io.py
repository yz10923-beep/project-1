import csv
from datetime import datetime

from riskreport.model import Trade

_SIDES = {"BUY", "SELL"}


def load_trades(path: str) -> tuple[list[Trade], int]:
    trades: list[Trade] = []
    rejected = 0
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                side = row["side"]
                qty = int(row["qty"])
                price = float(row["price"])
                ts = datetime.fromisoformat(row["ts"].strip())
            except (KeyError, TypeError, ValueError, AttributeError):
                rejected += 1
                continue
            if qty <= 0 or side not in _SIDES:
                rejected += 1
                continue
            trades.append(Trade(ts, row["symbol"].strip().upper(), side, qty, price))
    trades.sort(key=lambda t: t.ts)
    return trades, rejected


def load_prices(path: str) -> dict[str, float]:
    with open(path, newline="") as fh:
        return {r["symbol"].strip().upper(): float(r["price"]) for r in csv.DictReader(fh)}
