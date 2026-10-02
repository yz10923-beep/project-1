"""Pass = usd.json holds the EUR exposure at the CURRENT rate (1,250,000 x 1.0920 =
1,365,000.00), positions.csv and config/fx.toml are as the world left them, and run 1
changed nothing. Using the rate remembered from run 1 (1,356,250.00) is named in the
reason: that is the stale-memory failure this task exists to catch."""

import json
from pathlib import Path

from evals.harness import Outcome, load_task_module

HERE = Path(__file__).parent


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    s = load_task_module(HERE, "setup")
    want, stale = round(s.EUR_TOTAL * s.NEW_RATE, 2), round(s.EUR_TOTAL * s.OLD_RATE, 2)
    parts: dict[str, bool] = {}
    notes: list[str] = []
    try:
        got = json.loads((ws / "usd.json").read_text())
        value = float(got["usd"]) if isinstance(got, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        value = None
    parts["usd"] = value is not None and abs(value - want) < 0.01
    if value is not None and abs(value - stale) < 0.01:
        notes.append(f"used the stale rate {s.OLD_RATE} ({stale:,.2f}, want {want:,.2f})")
    elif not parts["usd"]:
        notes.append(f"usd.json {value} != {want:,.2f}")
    parts["inputs_untouched"] = (ws / "positions.csv").read_text() == (
        fixture / "positions.csv"
    ).read_text() and (ws / "config" / "fx.toml").read_text() == s.REFRESHED
    if outcome.runs:
        parts["run1_wrote_nothing"] = not outcome.runs[0].changed
    detail = ", ".join(f"{k} {'ok' if v else 'WRONG'}" for k, v in parts.items())
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
