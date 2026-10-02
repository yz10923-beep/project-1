"""Pass = incident.json names the right venue and reason code, run 1 changed no files
(it was asked only to answer), and run 2 never touched the log again: it used what the
session already knew. The reason lists each part."""

import json
from pathlib import Path

from evals.harness import Outcome, load_task_module

HERE = Path(__file__).parent


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    venue, reason = load_task_module(HERE, "setup").truth()
    parts: dict[str, bool] = {}
    notes: list[str] = []
    try:
        got = json.loads((ws / "incident.json").read_text())
        parts["answer"] = (
            isinstance(got, dict)
            and str(got.get("venue", "")).strip().upper() == venue
            and str(got.get("reason_code", "")).strip().upper() == reason
        )
        if not parts["answer"]:
            notes.append(f"incident.json {got} != {venue}/{reason}")
    except (OSError, ValueError):
        parts["answer"] = False
        notes.append("no valid incident.json")
    parts["log_untouched"] = "logs/oms.log" not in outcome.changed
    if len(outcome.runs) >= 2:
        parts["run1_wrote_nothing"] = not outcome.runs[0].changed
        parts["run2_no_reread"] = not outcome.runs[1].touched("oms.log")
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
