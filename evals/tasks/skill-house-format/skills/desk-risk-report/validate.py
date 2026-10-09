"""Checks a desk risk report's format (not its numbers): python3 validate.py REPORT.csv"""

import csv
import re
import sys

HEADER = ["desk", "gross_usd", "net_usd", "var99_usd", "limit_usd", "util_pct", "status"]
MONEY = re.compile(r"^-?\d+$")
PCT = re.compile(r"^\d+\.\d$")


def problems(path: str) -> list[str]:
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] != HEADER:
        return [f"header must be exactly {','.join(HEADER)}"]
    body, total = rows[1:-1], rows[-1] if len(rows) > 1 else []
    out = []
    if not body:
        out.append("no desk rows")
    for r in body:
        if len(r) != len(HEADER):
            out.append(f"row {r}: {len(HEADER)} fields expected")
            continue
        desk, gross, net, var, limit, util, status = r
        for name, v in (
            ("gross_usd", gross),
            ("net_usd", net),
            ("var99_usd", var),
            ("limit_usd", limit),
        ):
            if not MONEY.match(v):
                out.append(f"{desk}: {name} {v!r} is not whole dollars without separators")
        if not PCT.match(util):
            out.append(f"{desk}: util_pct {util!r} needs one decimal place")
            continue
        want = "BREACH" if float(util) > 100.0 else "WARN" if float(util) >= 85.0 else "OK"
        if status != want:
            out.append(f"{desk}: status {status!r} but util_pct {util} means {want}")
    utils = [float(r[5]) for r in body if len(r) == len(HEADER) and PCT.match(r[5])]
    if utils != sorted(utils, reverse=True):
        out.append("rows must be ordered by util_pct, highest first")
    if not total or total[0] != "TOTAL":
        out.append("the last row must be TOTAL")
    elif total[3:] != ["", "", "", ""]:
        out.append("TOTAL: var99_usd, limit_usd, util_pct and status must be empty")
    elif not (MONEY.match(total[1]) and MONEY.match(total[2])):
        out.append("TOTAL: gross_usd and net_usd must be whole dollars")
    elif body and all(
        len(r) == len(HEADER) and MONEY.match(r[1]) and MONEY.match(r[2]) for r in body
    ):
        if int(total[1]) != sum(int(r[1]) for r in body) or int(total[2]) != sum(
            int(r[2]) for r in body
        ):
            out.append("TOTAL: gross_usd and net_usd must be the column sums")
    return out


if __name__ == "__main__":
    found = problems(sys.argv[1])
    print("\n".join(found) if found else "OK")
    sys.exit(1 if found else 0)
