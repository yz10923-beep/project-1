import csv

from strategy import signal


def run(path: str) -> float:
    with open(path) as fh:
        spread = [float(row["spread"]) for row in csv.DictReader(fh)]
    pnl, pos = 0.0, 0
    for i in range(20, len(spread)):
        pnl += pos * (spread[i] - spread[i - 1])
        pos = signal(spread[: i + 1])
    return pnl
