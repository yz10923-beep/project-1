"""Pass = incident.json has the right start, dependency and count, the log is
byte-identical to what was generated, and the run didn't end with a context overflow.

The answer key is setup.truth() over freshly generated records. Formatting is lenient
(alt/): the start may drop the `T`/`Z`, case and whitespace are ignored, and the count
may be a numeric string. Content is strict. The reason lists every part.
"""

import hashlib
import json
from pathlib import Path

from evals.harness import Outcome, load_task_module

_setup = load_task_module(Path(__file__).parent, "setup")


def _norm(value: object) -> str:
    return " ".join(str(value).strip().strip('"').split()).lower()


def _norm_ts(value: object) -> str:
    return _norm(value).replace("t", " ").removesuffix("z").strip()


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    parts: dict[str, bool] = {}
    notes: list[str] = []
    want = _setup.truth()
    try:
        got = json.loads((ws / "incident.json").read_text())
        if not isinstance(got, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        got = {}
        notes.append(f"no valid incident.json ({e})")
    parts["start"] = bool(got) and _norm_ts(got.get("start", "")) == _norm_ts(want["start"])
    parts["dependency"] = bool(got) and _norm(got.get("dependency", "")) == want["dependency"]
    parts["rejected"] = _as_int(got.get("rejected")) == want["rejected"]
    log = ws / _setup.LOG_PATH
    parts["log_untouched"] = (
        log.is_file() and hashlib.sha256(log.read_bytes()).hexdigest() == _setup.log_sha256()
    )
    statuses = [r.status for r in outcome.runs] or [outcome.status]
    parts["no_overflow"] = "context_overflow" not in statuses
    detail = ", ".join(
        f"{k} {'ok' if ok else 'WRONG' + (f' (got {got.get(k)!r})' if k in got else '')}"
        for k, ok in parts.items()
    )
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
