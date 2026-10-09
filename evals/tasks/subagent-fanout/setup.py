"""Generates logs/<service>.log for eight trading services on 2024-03-15, from a fixed
seed: ~20k entries each, in four formats, the way they reach a log pipeline before any
normalization. A cascading outage hits five of them between 12:58 and 13:11 UTC.

Each service is its own small investigation, which is what makes the task fan out:

  order-router     JSON lines, ISO UTC                    incident
  risk-engine      logfmt (ts=, level=error)              incident; earlier near-miss
                                                          burst of 19 errors in 45s
  market-data      syslog, no year, whole seconds         incident
  settlement       Java, multi-line stack traces          no incident: 14 errors in 50s,
                                                          but 3 lines say ERROR each
  pricing          JSON, epoch milliseconds, lvl=E        incident
  position-keeper  logfmt, +05:30 offsets                 incident (18:40 local)
  fix-gateway      syslog                                 no incident: 48 errors over 4
                                                          minutes, at most 13 a minute
  reporting        Java                                   no incident: a WARN burst
                                                          whose text says "error"

`truth()` applies the goal's definition to the generated records; check.py never
trusts the agent's copy of the logs.
"""

from __future__ import annotations

import functools
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

SEED = 20240316
DAY = datetime(2024, 3, 15, tzinfo=UTC)
OPEN, CLOSE = DAY.replace(hour=6), DAY.replace(hour=20)
BACKGROUND = 19_000
WINDOW = timedelta(seconds=60)
THRESHOLD = 20
IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class Service:
    name: str
    fmt: str  # json | logfmt | syslog | java | epoch | logfmt_ist
    codes: tuple[str, ...]  # sporadic error codes
    incident: tuple[datetime, str] | None = None  # (start, code of its first error)


def _t(h: int, m: int, s: int, ms: int = 0) -> datetime:
    return DAY.replace(hour=h, minute=m, second=s, microsecond=ms * 1000)


SERVICES = (
    Service("order-router", "json", ("OR-400", "OR-409"), (_t(13, 2, 11, 512), "OR-504")),
    Service("risk-engine", "logfmt", ("RE-STALE", "RE-LIMIT"), (_t(13, 5, 40, 118), "RE-TIMEOUT")),
    Service("market-data", "syslog", ("MD-DROP", "MD-LATE"), (_t(12, 58, 3), "MD-GAP")),
    Service("settlement", "java", ("STL-RETRY", "STL-NACK")),
    Service("pricing", "epoch", ("PX-WIDE", "PX-MISS"), (_t(13, 1, 47, 903), "PX-STALE")),
    Service("position-keeper", "logfmt_ist", ("PK-SKEW", "PK-DUP"), (_t(13, 10, 2, 4), "PK-LOCK")),
    Service("fix-gateway", "syslog", ("FX-REJ", "FX-SEQ")),
    Service("reporting", "java", ("RP-EXPORT", "RP-QUOTA")),
)
NEAR_MISS = ("risk-engine", _t(11, 20, 5, 220), "RE-SLOW")  # 19 errors in 45s
STL_CLUSTER = _t(13, 15, 2, 118)  # settlement: 14 errors in 50s, 3 "ERROR" lines each
FIX_BURN = _t(14, 30, 0)  # fix-gateway: an error every 5s for 4 minutes
RP_WARNS = _t(13, 20, 10, 400)  # reporting: 60 WARN lines in 40s mentioning "error"

MESSAGES = {
    "DEBUG": ("cache refresh", "tick processed", "heartbeat sent", "queue depth ok"),
    "INFO": ("order accepted", "session alive", "snapshot published", "batch committed"),
    "WARN": ("slow upstream response", "retrying request", "high queue depth"),
    "ERROR": ("request failed", "upstream timed out", "dependency unavailable"),
}


@dataclass(frozen=True)
class Entry:
    ts: datetime
    level: str  # DEBUG | INFO | WARN | ERROR
    code: str | None
    msg: str


def _whole_seconds(svc: Service) -> bool:
    return svc.fmt == "syslog"


@functools.cache
def records() -> dict[str, tuple[Entry, ...]]:
    rng = random.Random(SEED)
    out: dict[str, tuple[Entry, ...]] = {}
    span = (CLOSE - OPEN).total_seconds()
    for svc in SERVICES:
        entries: list[Entry] = []
        for _ in range(BACKGROUND):
            level = rng.choices(("DEBUG", "INFO", "WARN"), (30, 61, 9))[0]
            ts = OPEN + timedelta(milliseconds=rng.randrange(int(span * 1000)))
            code = rng.choice(svc.codes) if level == "WARN" else None
            entries.append(Entry(ts, level, code, rng.choice(MESSAGES[level])))
        # isolated errors, at least 10 minutes apart and away from every burst
        avoid = [s for s in (svc.incident[0] if svc.incident else None,) if s]
        avoid += [NEAR_MISS[1]] if svc.name == NEAR_MISS[0] else []
        avoid += {"settlement": [STL_CLUSTER], "fix-gateway": [FIX_BURN], "reporting": []}.get(
            svc.name, []
        )
        sporadic: list[datetime] = []
        while len(sporadic) < 25:
            ts = OPEN + timedelta(milliseconds=rng.randrange(int(span * 1000)))
            near = sporadic + avoid
            if all(abs((ts - x).total_seconds()) > 600 for x in near) and not any(
                -60 <= (ts - a).total_seconds() <= 1200 for a in avoid
            ):
                sporadic.append(ts)
        for ts in sporadic:
            entries.append(Entry(ts, "ERROR", rng.choice(svc.codes), rng.choice(MESSAGES["ERROR"])))
        if svc.incident:
            start, code = svc.incident
            ts = start
            for i in range(rng.randrange(90, 220)):
                c = code if i == 0 or rng.random() < 0.6 else rng.choice(svc.codes)
                entries.append(Entry(ts, "ERROR", c, rng.choice(MESSAGES["ERROR"])))
                ts += timedelta(milliseconds=rng.randrange(200, 2400 if i < 40 else 6000))
        if svc.name == NEAR_MISS[0]:
            ts = NEAR_MISS[1]
            for _ in range(19):
                entries.append(Entry(ts, "ERROR", NEAR_MISS[2], "margin calc slow"))
                ts += timedelta(milliseconds=2400)
        if svc.name == "settlement":
            ts = STL_CLUSTER
            for _ in range(14):
                entries.append(Entry(ts, "ERROR", "STL-FAIL", "settlement batch failed"))
                ts += timedelta(milliseconds=3700)
        if svc.name == "fix-gateway":
            for i in range(48):
                entries.append(
                    Entry(
                        FIX_BURN + timedelta(seconds=5 * i),
                        "ERROR",
                        "FX-REJ",
                        "order rejected by venue",
                    )
                )
        if svc.name == "reporting":
            for i in range(60):
                entries.append(
                    Entry(
                        RP_WARNS + timedelta(milliseconds=660 * i),
                        "WARN",
                        "RP-EXPORT",
                        "export retry after error: timeout",
                    )
                )
        if _whole_seconds(svc):
            entries = [Entry(e.ts.replace(microsecond=0), e.level, e.code, e.msg) for e in entries]
        entries.sort(key=lambda e: e.ts)
        out[svc.name] = tuple(entries)
    return out


def _lines(svc: Service, e: Entry, i: int) -> list[str]:
    iso = e.ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{e.ts.microsecond // 1000:03d}Z"
    if svc.fmt == "json":
        row = {"ts": iso, "level": e.level, "service": svc.name, "msg": e.msg}
        return [json.dumps(row | ({"code": e.code} if e.code else {}))]
    if svc.fmt == "epoch":
        row = {"t": int(e.ts.timestamp() * 1000), "lvl": e.level[0], "service": svc.name}
        return [json.dumps(row | ({"err": e.code} if e.code else {}) | {"detail": e.msg})]
    if svc.fmt == "logfmt":
        code = f" code={e.code}" if e.code else ""
        return [f'ts={iso} level={e.level.lower()}{code} msg="{e.msg}"']
    if svc.fmt == "logfmt_ist":
        local = e.ts.astimezone(IST)
        stamp = local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}+05:30"
        code = f" code={e.code}" if e.code else ""
        return [f'time={stamp} lvl={e.level}{code} msg="{e.msg}"']
    if svc.fmt == "syslog":
        host = f"{svc.name.split('-')[0]}-host-{1 + i % 3}"
        tag = f" [{e.code}]" if e.code else ""
        return [f"{e.ts:%b %d %H:%M:%S} {host} {svc.name}[{2200 + i % 7}]: {e.level}{tag} {e.msg}"]
    # java
    stamp = e.ts.strftime("%Y-%m-%d %H:%M:%S,") + f"{e.ts.microsecond // 1000:03d}"
    level = e.level.ljust(5)
    lines = [
        f"{stamp} {level} [{svc.name}-worker-{1 + i % 4}] c.b.{svc.name.split('-')[0]}.Main - "
        f"{e.code + ' ' if e.code else ''}{e.msg}"
    ]
    if e.level == "ERROR":
        lines += [
            f"com.bank.{svc.name.split('-')[0]}.ServiceException: ERROR {e.code} {e.msg}",
            f"\tat com.bank.{svc.name.split('-')[0]}.Main.run(Main.java:{100 + i % 300})",
            "Caused by: java.net.SocketTimeoutException: ERROR 504 from upstream",
            "\tat java.base/java.net.Socket.read(Socket.java:1001)",
        ]
    return lines


@functools.cache
def texts() -> dict[str, str]:
    out = {}
    for svc in SERVICES:
        lines: list[str] = []
        for i, e in enumerate(records()[svc.name]):
            lines += _lines(svc, e, i)
        out[svc.name] = "\n".join(lines) + "\n"
    return out


def setup(ws: Path) -> None:
    logs = ws / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, text in texts().items():
        (logs / f"{name}.log").write_text(text)


def incident_start(entries: tuple[Entry, ...]) -> Entry | None:
    """The goal's definition: the earliest ERROR entry with at least THRESHOLD ERROR
    entries (itself included) in [its time, its time + 60s]."""
    errors = [e for e in entries if e.level == "ERROR"]
    j = 0
    for i, e in enumerate(errors):
        j = max(j, i)
        while j + 1 < len(errors) and errors[j + 1].ts - e.ts <= WINDOW:
            j += 1
        if j - i + 1 >= THRESHOLD:
            return e
    return None


@functools.cache
def truth() -> dict[str, dict[str, str]]:
    out = {}
    for name, entries in records().items():
        if (e := incident_start(entries)) is not None:
            out[name] = {"start": e.ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "code": str(e.code)}
    return out


def logs_sha256() -> str:
    h = hashlib.sha256()
    for name, text in sorted(texts().items()):
        h.update(name.encode() + b"\0" + hashlib.sha256(text.encode()).digest())
    return h.hexdigest()
