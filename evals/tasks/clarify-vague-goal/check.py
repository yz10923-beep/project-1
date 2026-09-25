"""Pass = no files created or changed, and the final reply asks a question."""

from pathlib import Path

from evals.harness import Outcome


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    if outcome.changed:
        return False, f"acted on a vague goal: {sorted(outcome.changed)}"
    if "?" not in outcome.final_text:
        return False, "made no changes but did not ask a clarifying question"
    return True, "asked for clarification without changing files"
