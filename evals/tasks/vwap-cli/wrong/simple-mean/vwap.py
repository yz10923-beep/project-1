import csv
import sys
from collections import defaultdict

prices: dict[tuple[str, str], list[float]] = defaultdict(list)
with open(sys.argv[1], newline="") as f:
    for row in csv.DictReader(f):
        prices[row["timestamp"][:16], row["symbol"]].append(float(row["price"]))
print("minute,symbol,vwap")
for minute, symbol in sorted(prices):
    p = prices[minute, symbol]
    print(f"{minute},{symbol},{sum(p) / len(p):.4f}")
