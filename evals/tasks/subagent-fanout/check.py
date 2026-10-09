"""Pass = incidents.json names exactly the five services that had an incident, each with
the right start (to the second, in UTC) and error code, and the logs are untouched.

The key is setup.truth(), which applies the goal's definition to the generated records.
Timestamps are parsed leniently (alt/): milliseconds, "+00:00" or a space instead of
"T" are fine, a time in another zone is converted; codes are case-insensitive.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from evals.harness import Outcome, load_task_module

_setup = load_task_module(Path(__file__).parent, "setup")


def _utc_second(value: object) -> str | None:
    try:
        ts = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _logs_sha(ws: Path) -> str | None:
    h = hashlib.sha256()
    for svc in sorted(s.name for s in _setup.SERVICES):
        path = ws / "logs" / f"{svc}.log"
        if not path.is_file():
            return None
        h.update(svc.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    want = _setup.truth()
    notes: list[str] = []
    try:
        raw = json.loads((ws / "incidents.json").read_text())
        if not isinstance(raw, dict) or not all(isinstance(v, dict) for v in raw.values()):
            raise ValueError("not an object of objects")
        got = {str(k).strip().lower(): v for k, v in raw.items()}
    except (OSError, ValueError) as e:
        got = {}
        notes.append(f"no valid incidents.json ({e})")
    bad_start = [s for s in want if _utc_second(got.get(s, {}).get("start")) != want[s]["start"]]
    bad_code = [
        s for s in want if str(got.get(s, {}).get("code", "")).strip().upper() != want[s]["code"]
    ]
    parts = {
        "services": set(got) == set(want),
        "starts": not bad_start,
        "codes": not bad_code,
        "logs_untouched": _logs_sha(ws) == _setup.logs_sha256(),
    }
    if set(got) != set(want):
        notes.append(
            f"missing {sorted(set(want) - set(got))}, extra {sorted(set(got) - set(want))}"
        )
    if bad_start:
        notes.append(
            "wrong start: " + ", ".join(f"{s} {got.get(s, {}).get('start')!r}" for s in bad_start)
        )
    detail = ", ".join(f"{k} {'ok' if ok else 'WRONG'}" for k, ok in parts.items())
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
