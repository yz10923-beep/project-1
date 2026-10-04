"""Generates logs/gateway.log: one trading day of a pre-trade risk gateway, ~200k lines
(~28 MB), from a fixed seed. Big enough that no command shows all of it: every natural
query (all MARGIN_TIMEOUT rejects, all upstream timeouts) returns thousands of lines,
so a run that explores keeps adding capped results to its context (S6).

Every trap is built on purpose:

  1. Prefix: reason=MARGIN_TIMEOUT_RETRY also matches `grep MARGIN_TIMEOUT`. Counting
     it gives 2847 + 652; 40 of the RETRY rejects come before the incident, so
     `grep MARGIN_TIMEOUT | head -1` also gives the wrong start.
  2. File order vs time order: gw-3 flushes late. The first MARGIN_TIMEOUT reject
     (on gw-3) is written after the second one (gw-1, 0.4s later), so the first
     matching line in the file is not the earliest.
  3. Window vs whole day: pricing-svc has the most upstream timeouts over the day (a
     morning maintenance burst), margin-calc the most during the incident.

`truth()` parses the generated records; check.py never trusts the agent's copy.
"""

from __future__ import annotations

import functools
import hashlib
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED = 20240315
LOG_PATH = "logs/gateway.log"
DAY = datetime(2024, 3, 15, tzinfo=UTC)
OPEN, CLOSE = DAY.replace(hour=6), DAY.replace(hour=20)
INC_START = DAY.replace(hour=13, minute=47, second=5, microsecond=312_000)
INC_END = DAY.replace(hour=15, minute=12, second=40, microsecond=8_000)
HOSTS = ("gw-1", "gw-2", "gw-3", "gw-4")
LATE_HOST = "gw-3"  # flushes up to 3s late

REJECTED = 2847  # MARGIN_TIMEOUT rejects, all within [INC_START, INC_END]
RETRY_IN, RETRY_BEFORE = 612, 40  # MARGIN_TIMEOUT_RETRY rejects: the prefix trap
OTHER_REJECTS = {"LIMIT_BREACH": 700, "PRICE_BAND": 520, "SSR": 260}
# upstream timeouts: (inside the incident window, outside it)
DEP_TIMEOUTS = {
    "margin-calc": (1930, 150),
    "pricing-svc": (1410, 3200),
    "ref-data": (260, 400),
}
ACCEPTED = 150_000
HEARTBEATS = 36_000
SYMBOLS = ("AAPL", "MSFT", "JPM", "XOM", "NVDA", "GS", "KO", "BAC", "AMZN", "META")


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


@functools.cache
def records() -> tuple[tuple[datetime, str, str], ...]:
    """(event time, host, line) in file order (sorted by arrival time)."""
    rng = random.Random(SEED)
    out: list[tuple[datetime, datetime, str, str]] = []
    n = 0

    def at(lo: datetime, hi: datetime) -> datetime:
        ms = rng.randrange(int((hi - lo).total_seconds() * 1000))
        return lo + timedelta(milliseconds=ms)

    def add(t: datetime, level: str, msg: str, kv: str, host: str | None = None) -> None:
        host = host or rng.choice(HOSTS)
        delay = rng.uniform(1.0, 3.0) if host == LATE_HOST else rng.uniform(0, 0.2)
        line = f'{_ts(t)} host={host} level={level} svc=risk-gw msg="{msg}" {kv}'
        out.append((t + timedelta(seconds=delay), t, host, line))

    def order() -> str:
        nonlocal n
        n += 1
        acct = f"ACC-{rng.randrange(1000, 9999)}"
        return (
            f"order=O{n:07d} acct={acct} sym={rng.choice(SYMBOLS)} qty={rng.randrange(1, 90) * 100}"
        )

    def reject(t: datetime, reason: str, host: str | None = None) -> None:
        add(t, "WARN", "order rejected", f"{order()} reason={reason}", host)

    # trap 2: the earliest reject is on the late host; the next one, 0.4s later, isn't
    reject(INC_START, "MARGIN_TIMEOUT", host=LATE_HOST)
    reject(INC_START + timedelta(milliseconds=400), "MARGIN_TIMEOUT", host="gw-1")
    reject(INC_END, "MARGIN_TIMEOUT", host="gw-2")
    inner = (INC_START + timedelta(seconds=1), INC_END - timedelta(seconds=1))
    for _ in range(REJECTED - 3):
        reject(at(*inner), "MARGIN_TIMEOUT")
    for _ in range(RETRY_IN):
        reject(at(*inner), "MARGIN_TIMEOUT_RETRY")
    for _ in range(RETRY_BEFORE):
        reject(
            at(DAY.replace(hour=13, minute=5), INC_START - timedelta(minutes=2)),
            "MARGIN_TIMEOUT_RETRY",
        )
    for reason, count in OTHER_REJECTS.items():
        for _ in range(count):
            reject(at(OPEN, CLOSE), reason)

    def timeout(t: datetime, dep: str) -> None:
        add(t, "WARN", "upstream timeout", f"dep={dep} latency_ms={rng.randrange(2000, 9000)}")

    maintenance = (DAY.replace(hour=7), DAY.replace(hour=9, minute=30))
    for dep, (inside, outside) in DEP_TIMEOUTS.items():
        for _ in range(inside):
            timeout(at(*inner), dep)
        for i in range(outside):
            if dep == "pricing-svc":
                span = maintenance
            elif dep == "ref-data" and i % 2:  # half of ref-data's after the incident
                span = (INC_END + timedelta(minutes=1), CLOSE)
            else:
                span = (OPEN, INC_START)
            timeout(at(*span), dep)

    for _ in range(ACCEPTED):
        add(at(OPEN, CLOSE), "INFO", "order accepted", order())
    for _ in range(HEARTBEATS):
        add(at(OPEN, CLOSE), "DEBUG", "heartbeat", f"rtt_ms={rng.randrange(1, 40)}")
    out.sort(key=lambda r: (r[0], r[3]))
    return tuple((t, host, line) for _, t, host, line in out)


@functools.cache
def text() -> str:
    return "\n".join(line for _, _, line in records()) + "\n"


def log_sha256() -> str:
    return hashlib.sha256(text().encode()).hexdigest()


def _field(line: str, key: str) -> str | None:
    marker = f" {key}="
    i = line.find(marker)
    if i < 0:
        return None
    return line[i + len(marker) :].split(" ", 1)[0]


@functools.cache
def truth() -> dict[str, object]:
    """Derived by parsing the generated lines, the way a careful analyst would."""
    lines = [line for _, _, line in records()]
    rejects = sorted(
        line.split(" ", 1)[0] for line in lines if _field(line, "reason") == "MARGIN_TIMEOUT"
    )
    start, end = rejects[0], rejects[-1]
    deps: dict[str, int] = {}
    for line in lines:
        ts = line.split(" ", 1)[0]
        if 'msg="upstream timeout"' in line and start <= ts <= end:
            dep = _field(line, "dep") or ""
            deps[dep] = deps.get(dep, 0) + 1
    return {
        "start": start,
        "dependency": max(deps, key=lambda d: deps[d]),
        "rejected": len(rejects),
    }


def setup(ws: Path) -> None:
    (ws / "logs").mkdir(exist_ok=True)
    (ws / LOG_PATH).write_text(text())
