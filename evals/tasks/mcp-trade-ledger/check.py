"""Pass = desk_notional.json has exactly the desks that traded on 2024-03-15, each within
$1 of the gross USD notional recomputed from the seeded ledger (evals/mcp/ledger_data.py,
which the server serves; the agent never sees this module).

One part per desk, so a trap shows where it bit: reading one page undercounts every
desk; multiplying by the USDJPY quote inflates the three desks that trade yen. Keys are
matched case-insensitively and values may be numeric strings or carry cents (alt/).
"""

import json
from pathlib import Path

from evals.harness import Outcome
from evals.mcp import ledger_data


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace("$", ""))
        except ValueError:
            return None
    return None


def check(ws: Path, outcome: Outcome, fixture: Path) -> tuple[bool, str]:
    want = ledger_data.gross_by_desk(ledger_data.trades())
    notes: list[str] = []
    try:
        raw = json.loads((ws / "desk_notional.json").read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        got = {str(k).strip().lower(): v for k, v in raw.items()}
    except (OSError, ValueError) as e:
        got = {}
        notes.append(f"no valid desk_notional.json ({e})")
    parts: dict[str, bool] = {"desks": set(got) == set(want)}
    for desk, usd in want.items():
        value = _num(got.get(desk))
        parts[desk.replace("-", "_")] = value is not None and abs(value - usd) <= 1.0
    detail = ", ".join(f"{k} {'ok' if ok else 'WRONG'}" for k, ok in parts.items())
    wrong = {d: got.get(d) for d in want if not parts[d.replace("-", "_")] and d in got}
    if wrong:
        notes.append(f"got {wrong}, want {{{', '.join(f'{d}: {want[d]}' for d in wrong)}}}")
    if extra := set(got) - set(want):
        notes.append(f"unexpected desks {sorted(extra)}")
    return all(parts.values()), detail + (f" · {'; '.join(notes)}" if notes else "")
