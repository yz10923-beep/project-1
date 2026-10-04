"""Pass = morning_note.json carries run 1's morning answer (account, and USD within $1)
and the three breach counts, watchlist.txt kept its lines and gained the right account
once, run 1 changed nothing, and no run overflowed. The answer key is setup.truth();
the reason lists every part, so a failure shows which fact was lost."""

import json
from pathlib import Path

from evals.harness import Outcome, load_task_module

HERE = Path(__file__).parent
_setup = load_task_module(HERE, "setup")


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    want = _setup.truth()
    parts: dict[str, bool] = {}
    notes: list[str] = []
    try:
        got = json.loads((ws / "morning_note.json").read_text())
        if not isinstance(got, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        got = {}
        notes.append(f"no valid morning_note.json ({e})")
    largest = got.get("largest_exposure") if isinstance(got.get("largest_exposure"), dict) else {}
    want_l = want["largest_exposure"]
    parts["largest_account"] = str(largest.get("account", "")).strip().upper() == want_l["account"]
    usd = _num(largest.get("usd"))
    parts["largest_usd"] = usd is not None and abs(usd - want_l["usd"]) <= 1
    if largest and not (parts["largest_account"] and parts["largest_usd"]):
        notes.append(f"largest_exposure {largest}")
    breaches = got.get("breaches") if isinstance(got.get("breaches"), dict) else {}
    parts["breaches"] = all(_num(breaches.get(f)) == n for f, n in want["breaches"].items())
    if breaches and not parts["breaches"]:
        notes.append(f"breaches {breaches}")
    watch = (ws / "watchlist.txt").read_text().split() if (ws / "watchlist.txt").is_file() else []
    original = (fixture / "watchlist.txt").read_text().split()
    parts["watchlist"] = sorted(watch) == sorted([*original, want["watch"]])
    if len(outcome.runs) >= 1:
        parts["run1_wrote_nothing"] = not outcome.runs[0].changed
    statuses = [r.status for r in outcome.runs] or [outcome.status]
    parts["no_overflow"] = "context_overflow" not in statuses
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
