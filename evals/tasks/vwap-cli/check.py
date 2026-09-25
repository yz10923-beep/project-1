"""Pass = on a hidden trade file (not the sample), output matches an independent VWAP
computation exactly, and a header-only file prints only the header.

The hidden file mixes symbols, interleaves them in time, and puts trades at :59 and :00
so minute bucketing and sorting both matter. Seeded, so every trial sees the same data."""

import random
import re
from collections import defaultdict
from pathlib import Path

from evals.harness import Outcome, run_py

HEADER = "minute,symbol,vwap"


def _hidden_trades() -> list[tuple[str, str, float, int]]:
    rng = random.Random(20260925)
    trades = []
    for minute in range(4):
        for sec in (0, 13, 31, 59):
            for sym in ("MSFT", "AAPL", "NVDA"):
                if rng.random() < 0.7:
                    price = round(rng.uniform(100, 500), 2)
                    trades.append(
                        (f"2024-03-15T14:{minute:02d}:{sec:02d}Z", sym, price, rng.randint(1, 900))
                    )
    return trades


def _expected(trades: list[tuple[str, str, float, int]]) -> list[str]:
    pv: dict[tuple[str, str], float] = defaultdict(float)
    vol: dict[tuple[str, str], int] = defaultdict(int)
    for ts, sym, price, qty in trades:
        pv[ts[:16], sym] += price * qty
        vol[ts[:16], sym] += qty
    return [HEADER] + [f"{m},{s},{pv[m, s] / vol[m, s]:.4f}" for m, s in sorted(pv)]


def _same_row(got: str, want: str) -> bool:
    """Keys must match exactly; vwap must have exactly 4 decimals and be within one unit
    in the last place, so a correct solution that rounds a half-way value differently
    (Decimal vs float) is not failed."""
    g, w = got.split(","), want.split(",")
    if len(g) != 3 or g[:2] != w[:2] or not re.fullmatch(r"-?\d+\.\d{4}", g[2]):
        return False
    return abs(float(g[2]) - float(w[2])) <= 0.00011


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    if not (ws / "vwap.py").is_file():
        return False, "vwap.py not created"
    trades = _hidden_trades()
    (ws / "_hidden.csv").write_text(
        "timestamp,symbol,price,qty\n" + "".join(f"{t},{s},{p},{q}\n" for t, s, p, q in trades)
    )
    r = run_py(ws, "vwap.py", "_hidden.csv")
    got = [line.strip() for line in r.stdout.strip().splitlines()]
    want = _expected(trades)
    if r.returncode != 0:
        return False, f"exit {r.returncode}: {r.stderr.strip()[-200:]}"
    if len(got) != len(want) or got[:1] != [HEADER]:
        return False, f"got {len(got)} lines want {len(want)}; first line {got[:1]}"
    for i, (g, w) in enumerate(zip(got[1:], want[1:], strict=True), start=1):
        if not _same_row(g, w):
            return False, f"line {i}: got {g!r} want {w!r}"
    (ws / "_empty.csv").write_text("timestamp,symbol,price,qty\n")
    r = run_py(ws, "vwap.py", "_empty.csv")
    if r.returncode != 0 or r.stdout.strip() != HEADER:
        return False, f"header-only input: exit {r.returncode}, output {r.stdout.strip()[:80]!r}"
    return True, f"{len(want) - 1} buckets exact; header-only ok"
