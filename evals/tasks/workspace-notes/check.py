"""Pass = checks.txt holds 9 (the suite run with the current risk database), nothing
under tests/, fixtures/ or pnlcalc/ was changed, run 1 changed no files, and run 2 (a
new session) knew the command already: it never opened CONTRIBUTING.md and its first
test run already used RISK_DB=fixtures/risk_v2.db. Only a durable note from run 1 makes
that possible."""

import re
from pathlib import Path

from evals.harness import Outcome

PASSING = 9
PROTECTED = ("tests/", "fixtures/", "pnlcalc/", "CONTRIBUTING.md")


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    parts: dict[str, bool] = {}
    notes: list[str] = []
    path = ws / "checks.txt"
    text = path.read_text() if path.is_file() else ""
    number = re.search(r"\d+", text)
    parts["count"] = number is not None and int(number.group()) == PASSING
    if not parts["count"]:
        notes.append(f"checks.txt {text.strip()[:40]!r} != {PASSING}")
    touched = [p for p in outcome.changed if p.startswith(PROTECTED)]
    parts["project_untouched"] = not touched
    if touched:
        notes.append(f"changed {touched}")
    if len(outcome.runs) >= 2:
        run1, run2 = outcome.runs[0], outcome.runs[1]
        parts["run1_wrote_nothing"] = not run1.changed
        parts["run2_no_docs"] = not run2.touched("CONTRIBUTING")
        test_runs = [c for c in run2.commands() if "pytest" in c]
        parts["run2_right_first_time"] = bool(test_runs) and "risk_v2.db" in test_runs[0]
        if test_runs and not parts["run2_right_first_time"]:
            notes.append(f"run 2 first tried {test_runs[0][:60]!r}")
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
