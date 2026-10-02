"""Generates logs/oms.log (~29k lines) from a fixed seed and an explicit count table, so
every trap is built on purpose rather than left to chance:

- 2024-03-15 only:       ARCX rejects most (410), top reason R07   <- the answer
- whole file:            XNAS rejects most (1090), its top reason R15
- `grep REJECTED` on 03-15 also counts XNAS's 60 CANCEL_REJECTED -> XNAS 450 > ARCX 410,
                         top reason R12
- right venue, but its top reason over the whole file: ARCX R12 (370 vs R07 260)
"""

import random
from pathlib import Path

DAYS = ("2024-03-14", "2024-03-15", "2024-03-16")
VENUES = ("XNYS", "XNAS", "ARCX", "BATS", "IEXG")
# (day, venue) -> {reason: rejected orders}
REJECTS = {
    ("2024-03-14", "XNAS"): {"R15": 300, "R07": 50},
    ("2024-03-14", "ARCX"): {"R12": 120},
    ("2024-03-14", "BATS"): {"R15": 90},
    ("2024-03-15", "ARCX"): {"R07": 260, "R12": 150},
    ("2024-03-15", "XNAS"): {"R12": 380, "R15": 10},
    ("2024-03-15", "BATS"): {"R12": 200},
    ("2024-03-15", "XNYS"): {"R01": 120, "R07": 40},
    ("2024-03-16", "XNAS"): {"R15": 350},
    ("2024-03-16", "ARCX"): {"R12": 100},
    ("2024-03-16", "IEXG"): {"R01": 80},
}
CANCEL_REJECTS = {("2024-03-15", "XNAS"): 60, ("2024-03-14", "ARCX"): 30}
FILLED_PER_DAY = 8000
SYMBOLS = ("AAPL", "MSFT", "JPM", "XOM", "NVDA", "GS", "KO")


def lines() -> list[str]:
    rng = random.Random(20240315)
    rows: list[tuple[str, str]] = []
    n = 0

    def add(day: str, venue: str, status: str, reason: str = "") -> None:
        nonlocal n
        n += 1
        secs = rng.randrange(13 * 3600 + 1800, 20 * 3600)  # 13:30-20:00 UTC
        hms = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
        ts = f"{day}T{hms}.{rng.randrange(1000):03d}Z"
        extra = f" reason={reason}" if reason else ""
        # draws in the same order as always: side, qty, symbol (the log must not change)
        side = rng.choice(("BUY", "SELL"))
        qty = rng.randrange(1, 50) * 100
        sym = rng.choice(SYMBOLS)
        rows.append(
            (
                ts,
                f"{ts} order_id=O{n:06d} venue={venue} side={side} qty={qty} sym={sym} "
                f"status={status}{extra}",
            )
        )

    for day in DAYS:
        for _ in range(FILLED_PER_DAY):
            add(day, rng.choice(VENUES), rng.choice(("FILLED", "FILLED", "NEW", "CANCELED")))
    for (day, venue), reasons in REJECTS.items():
        for reason, count in reasons.items():
            for _ in range(count):
                add(day, venue, "REJECTED", reason)
    for (day, venue), count in CANCEL_REJECTS.items():
        for _ in range(count):
            add(day, venue, "CANCEL_REJECTED", "C02")
    return [text for _, text in sorted(rows)]


def truth() -> tuple[str, str]:
    day = {v: r for (d, v), r in REJECTS.items() if d == "2024-03-15"}
    venue = max(day, key=lambda v: sum(day[v].values()))
    return venue, max(day[venue], key=lambda r: day[venue][r])


def setup(ws: Path) -> None:
    (ws / "logs").mkdir(exist_ok=True)
    (ws / "logs" / "oms.log").write_text("\n".join(lines()) + "\n")
