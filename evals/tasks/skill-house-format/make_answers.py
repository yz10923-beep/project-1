"""Regenerates oracle/, wrong/ and alt/ from the fixture and the house rules. Each wrong/
answer breaks one rule; alt/ answers format the same content differently.

    uv run python evals/tasks/skill-house-format/make_answers.py
"""

import json
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.harness import load_task_module  # noqa: E402

HERE = Path(__file__).parent
check = load_task_module(HERE, "check")
LOADED = [{"tool_calls": [["load_skill", {"name": "desk-risk-report"}]]}]


def table(**tweak: Any) -> list[list[str]]:
    want = check.truth(HERE / "fixture")
    order = sorted(want, key=lambda d: d if tweak.get("by_desk") else -want[d]["util"])
    money = (lambda x: f"{round(x):,}") if tweak.get("separators") else (lambda x: str(round(x)))
    rows = [list(check.HEADER)]
    for d in order:
        w = want[d]
        net = w["gross"] if tweak.get("shorts_positive") else w["net"]
        rows.append(
            [
                d,
                money(w["gross"]),
                money(net),
                money(w["var"]),
                money(w["limit"]),
                f"{w['util']:.1f}",
                w["status"],
            ]
        )
    if not tweak.get("no_total"):
        gross = sum(int(r[1].replace(",", "")) for r in rows[1:])
        net = sum(int(r[2].replace(",", "")) for r in rows[1:])
        var = (
            str(sum(int(r[3].replace(",", "")) for r in rows[1:]))
            if tweak.get("var_summed")
            else ""
        )
        rows.append(["TOTAL", money(gross), money(net), var, "", "", ""])
    return rows


def csv_text(rows: list[list[str]], quote: bool = False, eol: str = "\n") -> str:
    def cell(c: str) -> str:
        return f'"{c}"' if quote or "," in c else c

    return "".join(",".join(cell(c) for c in r) + eol for r in rows)


def write(rel: str, text: str, name: str = check.REPORT, runs: Any = LOADED) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    (d / name).parent.mkdir(parents=True)
    (d / name).write_text(text, newline="")
    if runs is not None:
        (d / "_runs.json").write_text(json.dumps(runs) + "\n")


def main() -> None:
    write("oracle", csv_text(table()))
    write("wrong/dashed-date-name", csv_text(table()), name="reports/desk_risk_2024-03-22.csv")
    write("wrong/no-total-row", csv_text(table(no_total=True)))
    write("wrong/var-summed", csv_text(table(var_summed=True)))
    write("wrong/sorted-by-desk", csv_text(table(by_desk=True)))
    write("wrong/shorts-positive", csv_text(table(shorts_positive=True)))
    write("wrong/thousands-separators", csv_text(table(separators=True)))
    distracted = [
        {"tool_calls": [*LOADED[0]["tool_calls"], ["load_skill", {"name": "release-notes"}]]}
    ]
    write("wrong/loaded-distractor", csv_text(table()), runs=distracted)
    write("alt/quoted-fields", csv_text(table(), quote=True))
    write("alt/crlf", csv_text(table(), eol="\r\n"))


if __name__ == "__main__":
    main()
