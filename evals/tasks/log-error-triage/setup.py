"""Generates logs/app.log: a trading platform's merged service log, 08:00-11:00 UTC.

Every trap is constructed explicitly, not left to chance, so the task stays
discriminating however the random background comes out:

  1. Window vs whole file: market-data has the most ERRORs overall (noisy before 09:00
     and after 10:00), but not inside the 09:00-10:00 window.
  2. End boundary: settlement has 39 in-window ERRORs plus 3 stamped exactly
     10:00:00.000. Treating the end as inclusive gives settlement 42 > 41: wrong service.
  3. Start boundary: risk-engine's earliest in-window ERROR is at exactly 09:00:00.000.
     Treating the start as exclusive gives count 40 and the wrong first message.
  4. Level vs text: pricing logs 60 in-window WARN lines whose message contains
     "error". A case-insensitive grep for "error" crowns pricing.
  5. Time order vs file order: host rk-2 flushed late, so risk-engine's 09:00:00.000
     ERROR appears in the file *after* its 09:00:00.350 ERROR. "First line in the file"
     is not "earliest by timestamp".
  6. Noise: multi-line stack traces after some ERRORs, DEBUG/INFO chatter, ~60k lines.
     Too big to read through the tools (output is truncated at 30k chars), so the agent
     has to filter with grep/awk/python.

`truth()` derives the answer from the generated records. check.py calls it, so the
answer key never depends on the agent's copy of the file.
"""

from __future__ import annotations

import functools
import hashlib
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEED = 20240315
DAY = datetime(2024, 3, 15, tzinfo=UTC)
START, END = DAY.replace(hour=8), DAY.replace(hour=11)
WIN_START, WIN_END = DAY.replace(hour=9), DAY.replace(hour=10)  # [start, end)
LOG_PATH = "logs/app.log"

SERVICES = ["auth", "market-data", "order-gateway", "pricing", "risk-engine", "settlement"]
HOSTS = {
    "auth": ["au-1"],
    "market-data": ["md-1", "md-2"],
    "order-gateway": ["gw-1", "gw-2"],
    "pricing": ["px-1"],
    "risk-engine": ["rk-1", "rk-2"],
    "settlement": ["st-1"],
}
# ERROR counts inside the window. The winner leads the runner-up by 2, so boundary
# mistakes flip the answer.
IN_WINDOW_ERRORS = {
    "risk-engine": 41,
    "settlement": 39,
    "order-gateway": 25,
    "pricing": 12,
    "market-data": 10,
    "auth": 6,
}
OUT_OF_WINDOW_ERRORS = {"market-data": 120, "order-gateway": 8, "auth": 5}

FIRST_ERROR_MSG = "Position limit breached for account ACC-7731: exposure 1250000 > limit 1000000"
FILE_FIRST_MSG = "Margin call calculation timed out for book EQ-12 after 5000ms"


@dataclass
class Rec:
    ts: datetime
    host: str
    level: str
    service: str
    msg: str
    order: float  # position in the file; normally the timestamp
    trace: tuple[str, ...] = ()


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _error_msg(rng: random.Random, service: str) -> str:
    n = rng.randint(1000, 9999)
    return {
        "risk-engine": rng.choice(
            [
                f"Position limit breached for account ACC-{n}: "
                f"exposure {rng.randint(11, 30)}00000 > limit 1000000",
                f"Margin call calculation timed out for book EQ-{n % 97} after 5000ms",
                f"VaR model returned NaN for portfolio PF-{n}",
            ]
        ),
        "settlement": rng.choice(
            [
                f"Settlement instruction rejected by custodian: SSI mismatch for trade T-{n}",
                f"Failed to match trade T-{n} with counterparty confirmation",
            ]
        ),
        "order-gateway": f"Order O-{n} rejected: FIX session RBTN-{n % 13} not logged on",
        "pricing": f"Stale price for {rng.choice(['AAPL', 'MSFT', 'NVDA', 'ES'])}: "
        f"last tick {rng.randint(6, 90)}s ago",
        "market-data": f"Feed handler lost multicast group 239.1.1.{n % 50}; reconnecting",
        "auth": f"Token refresh failed for service account svc-{n % 40}",
    }[service]


def _chatter(rng: random.Random, service: str) -> tuple[str, str]:
    level = rng.choices(["DEBUG", "INFO", "WARN"], weights=[30, 60, 10])[0]
    n = rng.randint(1, 99999)
    msg = {
        "DEBUG": f"heartbeat seq={n}",
        "INFO": rng.choice(
            [
                f"processed batch {n} in {rng.randint(1, 80)}ms",
                f"order O-{n} acknowledged",
                f"quote update {n} published",
                "error budget healthy: 99.95% availability",
            ]
        ),
        "WARN": f"slow downstream call: {rng.randint(200, 2000)}ms",
    }[level]
    return level, msg


def _stack(rng: random.Random, service: str) -> tuple[str, ...]:
    cls = "".join(p.capitalize() for p in service.split("-"))
    pkg = service.replace("-", "")
    frames = [
        f"    at com.robothon.{pkg}.{cls}Worker.handle({cls}Worker.java:{rng.randint(20, 400)})"
    ]
    frames += [f"    at com.robothon.core.Dispatcher.run(Dispatcher.java:{rng.randint(20, 400)})"]
    if rng.random() < 0.5:
        frames.append(
            f"    Caused by: com.robothon.{service.replace('-', '')}.{cls}Error: see above"
        )
    return tuple(frames)


def _rand_ts(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    ms = rng.randrange(int((hi - lo).total_seconds() * 1000))
    return lo + timedelta(milliseconds=ms)


@functools.cache  # deterministic; callers must not mutate the result
def records() -> list[Rec]:
    rng = random.Random(SEED)
    out: list[Rec] = []
    ms = timedelta(milliseconds=1)

    # background chatter, ~5 lines/s
    t = START
    while t < END:
        svc = rng.choice(SERVICES)
        level, msg = _chatter(rng, svc)
        jitter = rng.uniform(0, 0.4) if level != "ERROR" else 0.0
        out.append(Rec(t, rng.choice(HOSTS[svc]), level, svc, msg, t.timestamp() + jitter))
        t += timedelta(milliseconds=rng.randint(50, 350))

    def error(ts: datetime, svc: str, msg: str | None = None, host: str | None = None) -> Rec:
        trace = _stack(rng, svc) if rng.random() < 0.3 else ()
        return Rec(
            ts,
            host or rng.choice(HOSTS[svc]),
            "ERROR",
            svc,
            msg or _error_msg(rng, svc),
            ts.timestamp(),
            trace,
        )

    # trap 3 + 5: risk-engine's earliest in-window ERROR is exactly 09:00:00.000 but sits in
    # the file after its 09:00:00.350 ERROR (late flush from rk-2).
    late = error(WIN_START + 350 * ms, "risk-engine", FILE_FIRST_MSG, "rk-1")
    first = error(WIN_START, "risk-engine", FIRST_ERROR_MSG, "rk-2")
    first.order = late.order + 0.001
    out += [late, first]
    # remaining in-window errors, strictly after those two
    for svc, n in IN_WINDOW_ERRORS.items():
        remaining = n - (2 if svc == "risk-engine" else 0)
        for _ in range(remaining):
            out.append(error(_rand_ts(rng, WIN_START + 400 * ms, WIN_END), svc))
    # trap 2: settlement errors stamped exactly at the exclusive end, and just before start
    for _ in range(3):
        out.append(error(WIN_END, "settlement", host="st-1"))
    for _ in range(2):
        out.append(error(WIN_START - ms, "settlement", host="st-1"))
    # trap 1: errors outside the window
    for svc, n in OUT_OF_WINDOW_ERRORS.items():
        for _ in range(n):
            lo, hi = (START, WIN_START) if rng.random() < 0.5 else (WIN_END + ms, END)
            out.append(error(_rand_ts(rng, lo, hi), svc))
    # trap 4: WARN lines that mention "error"
    for _ in range(60):
        ts = _rand_ts(rng, WIN_START, WIN_END)
        out.append(
            Rec(
                ts,
                "px-1",
                "WARN",
                "pricing",
                "retrying quote after upstream error (attempt 2/3)",
                ts.timestamp(),
            )
        )

    out.sort(key=lambda r: r.order)
    return out


def render(recs: list[Rec]) -> str:
    lines = []
    for r in recs:
        lines.append(
            f'{_fmt(r.ts)} host={r.host} level={r.level} service={r.service} msg="{r.msg}"'
        )
        lines.extend(r.trace)
    return "\n".join(lines) + "\n"


def truth(recs: list[Rec]) -> dict[str, object]:
    """The correct answer, computed from the records (never from the rendered file)."""
    in_win = [r for r in recs if r.level == "ERROR" and WIN_START <= r.ts < WIN_END]
    counts: dict[str, int] = {}
    for r in in_win:
        counts[r.service] = counts.get(r.service, 0) + 1
    service = max(counts, key=lambda s: counts[s])
    ranked = sorted(counts.values(), reverse=True)
    assert ranked[0] > ranked[1], "answer must be unique"
    earliest = min((r for r in in_win if r.service == service), key=lambda r: r.ts)
    return {"service": service, "count": counts[service], "first_error": earliest.msg}


@functools.cache
def log_text() -> str:
    return render(records())


@functools.cache
def log_sha256() -> str:
    return hashlib.sha256(log_text().encode()).hexdigest()


def setup(ws: Path) -> None:
    path = ws / LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(log_text())
