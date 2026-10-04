"""S6: a five-run session that outgrows a small budget (task.toml), so the history is
compacted at least once before run 5 needs facts from runs 1 and 2.

- Run 1 answers in chat only: the account with the largest gross exposure in the
  *morning* positions.csv, to the dollar. After run 1 the file is replaced with
  end-of-day positions (`between`), where another account is largest, so run 5 can't
  recover the morning answer by re-reading: it has to survive in the session.
- Runs 2-4 count per-trade limit breaches in three trade files (and list them, which
  is what makes the history grow). Exactly-at-limit trades are not breaches: `>=` gives
  different counts. Run 2 also adds an account to watchlist.txt.
- Run 5 writes morning_note.json from what the session knows.
"""

from __future__ import annotations

import functools
import hashlib
import random
from pathlib import Path

SEED = 20240316
LIMIT = 2_500_000
ACCOUNTS = [f"ACC-{n}" for n in (1042, 2207, 3318, 4471, 5530, 6604, 7129, 8815, 9263, 1188)]
PRICES = {
    "AAPL": 172.62,
    "MSFT": 415.10,
    "JPM": 191.27,
    "XOM": 113.08,
    "NVDA": 878.35,
    "GS": 389.54,
    "KO": 59.81,
    "BAC": 34.67,
    "AMZN": 174.42,
    "META": 484.10,
}
FILES = ("a", "b", "c")
MORNING_TOP, EOD_TOP = "ACC-2207", "ACC-5530"


def _positions(rng: random.Random) -> list[list[object]]:
    rows: list[list[object]] = []
    for acct in ACCOUNTS:
        for sym in rng.sample(sorted(PRICES), 6):
            qty = rng.randrange(-40, 60) * 100 or 100  # shorts count gross too
            rows.append([acct, sym, qty, PRICES[sym]])
    return rows


@functools.cache
def positions() -> tuple[str, str]:
    """(morning csv, end-of-day csv)."""
    rng = random.Random(SEED)
    morning = _positions(rng)
    morning.append([MORNING_TOP, "NVDA", -26_400, PRICES["NVDA"]])  # a big short
    eod = [list(r) for r in morning if not (r[0] == MORNING_TOP and r[2] == -26_400)]
    eod.append([MORNING_TOP, "NVDA", -2_400, PRICES["NVDA"]])  # mostly covered by the close
    eod.append([EOD_TOP, "MSFT", 61_000, PRICES["MSFT"]])
    rng.shuffle(morning)
    rng.shuffle(eod)

    def csv(rows: list[list[object]]) -> str:
        body = "".join(f"{a},{s},{q},{p:.2f}\n" for a, s, q, p in rows)
        return "account,symbol,qty,price\n" + body

    return csv(morning), csv(eod)


@functools.cache
def trades() -> dict[str, str]:
    """Each file: ~1200 trades; ~30-45 breaches; a few exactly at the limit."""
    rng = random.Random(SEED + 1)
    out: dict[str, str] = {}
    n = 0
    for name in FILES:
        rows: list[str] = []
        breaches = {"a": 37, "b": 29, "c": 44}[name]
        # file a: one account breaches most, unambiguously
        heavy = {"a": "ACC-8815", "b": "ACC-1042", "c": "ACC-4471"}[name]
        specs = [("normal", None)] * 1150 + [("breach", None)] * breaches + [("tie", None)] * 6
        for kind, _ in specs:
            n += 1
            sym = rng.choice(sorted(PRICES))
            px = PRICES[sym]
            if kind == "normal":
                qty = rng.randrange(1, max(2, int(LIMIT * 0.9 / px) // 100)) * 100
                acct = rng.choice(ACCOUNTS)
            elif kind == "breach":
                qty = (int(LIMIT / px) // 100 + rng.randrange(1, 40)) * 100
                acct = heavy if rng.random() < 0.45 else rng.choice(ACCOUNTS)
            else:  # exactly at the limit: 10,000 x 250.00
                sym, px, qty, acct = "TIE", 250.00, 10_000, rng.choice(ACCOUNTS)
            side = rng.choice(("BUY", "SELL"))
            rows.append(f"T{n:06d},{acct},{sym},{side},{qty},{px:.2f}")
        rng.shuffle(rows)
        out[name] = "trade_id,account,symbol,side,qty,price\n" + "\n".join(rows) + "\n"
    return out


def _gross(csv_text: str) -> dict[str, float]:
    gross: dict[str, float] = {}
    for line in csv_text.splitlines()[1:]:
        acct, _, qty, px = line.split(",")
        gross[acct] = gross.get(acct, 0.0) + abs(int(qty) * float(px))
    return gross


def _breaches(csv_text: str, strict: bool = True) -> list[tuple[str, str]]:
    out = []
    for line in csv_text.splitlines()[1:]:
        tid, acct, _, _, qty, px = line.split(",")
        notional = int(qty) * float(px)
        if notional > LIMIT or (not strict and notional == LIMIT):
            out.append((tid, acct))
    return out


@functools.cache
def truth() -> dict[str, object]:
    morning, _ = positions()
    gross = _gross(morning)
    top = max(gross, key=lambda a: gross[a])
    by_file = {f: _breaches(trades()[f]) for f in FILES}
    counts: dict[str, int] = {}
    for _, acct in by_file["a"]:
        counts[acct] = counts.get(acct, 0) + 1
    return {
        "largest_exposure": {"account": top, "usd": round(gross[top])},
        "breaches": {f: len(v) for f, v in by_file.items()},
        "watch": max(counts, key=lambda a: counts[a]),
    }


def inputs_sha256() -> str:
    h = hashlib.sha256()
    for part in (*positions(), *trades().values()):
        h.update(part.encode())
    return h.hexdigest()


def setup(ws: Path) -> None:
    (ws / "positions.csv").write_text(positions()[0])
    (ws / "trades").mkdir(exist_ok=True)
    for name, text in trades().items():
        (ws / "trades" / f"2024-03-15_{name}.csv").write_text(text)


def between(ws: Path, finished: int) -> None:
    if finished == 1:  # the end-of-day file replaces the morning one
        (ws / "positions.csv").write_text(positions()[1])
