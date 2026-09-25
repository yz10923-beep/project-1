import csv
import sys
from collections import defaultdict

pv: dict[tuple[str, str], float] = defaultdict(float)
vol: dict[tuple[str, str], float] = defaultdict(float)
with open(sys.argv[1], newline="") as f:
    for row in csv.DictReader(f):
        key = (row["timestamp"][:16], row["symbol"])
        pv[key] += float(row["price"]) * float(row["qty"])
        vol[key] += float(row["qty"])
print("minute,symbol,vwap")
for minute, symbol in sorted(pv):
    print(f"{minute},{symbol},{pv[minute, symbol] / vol[minute, symbol]:.4f}")
