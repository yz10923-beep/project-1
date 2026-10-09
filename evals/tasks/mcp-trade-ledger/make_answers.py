"""Regenerates oracle/, wrong/ and alt/ from the seeded ledger. Each wrong/ answer is
what one naive approach yields. Re-run after editing evals/mcp/ledger_data.py:

    uv run python evals/tasks/mcp-trade-ledger/make_answers.py
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.mcp import ledger_data as data  # noqa: E402

HERE = Path(__file__).parent


def write(rel: str, answer: dict[str, Any]) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "desk_notional.json").write_text(json.dumps(answer, indent=2) + "\n")


def gross(
    rows: list[dict[str, Any]], *, fx: Any = data.usd, cancelled: bool = False
) -> dict[str, int]:
    out: dict[str, float] = {}
    for t in rows:
        if cancelled or t["status"] == "BOOKED":
            amount = fx(abs(t["qty"] * t["price"]), t["ccy"], data.DATE)
            out[t["desk"]] = out.get(t["desk"], 0.0) + amount
    return {d: round(v) for d, v in sorted(out.items())}


def main() -> None:
    rows = data.trades()
    day = [t for t in rows if t["trade_date"] == data.DATE]
    right = data.gross_by_desk(rows)
    assert gross(day) == right

    def times_rate(amount: float, ccy: str, date: str) -> float:
        return amount if ccy == "USD" else amount * data.FX[date][ccy][1]

    answers = {
        "oracle": right,
        "wrong/first-page-only": gross(day[:50]),
        "wrong/jpy-multiplied": gross(day, fx=times_rate),
        "wrong/cancelled-included": gross(day, cancelled=True),
        "wrong/both-days": gross(rows),
        "wrong/local-currency": gross(day, fx=lambda a, c, d: a),
        "wrong/missing-desk": {k: v for k, v in right.items() if k != "fx-options"},
    }
    for rel, answer in answers.items():
        if rel != "oracle":
            assert answer != right, rel
        write(rel, answer)
    write("alt/strings-and-cents", {k.upper(): f"{v + 0.37:.2f}" for k, v in right.items()})


if __name__ == "__main__":
    main()
