"""Regenerates oracle/, wrong/ and alt/. Each wrong/ answer is what a specific naive
approach yields on the generated log, or a correct answer reached the wrong way
(re-reading the log in run 2, writing the file in run 1). Re-run after editing setup.py:

    uv run python evals/tasks/recall-across-runs/make_answers.py
"""

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.harness import load_task_module  # noqa: E402

HERE = Path(__file__).parent
setup = load_task_module(HERE, "setup")


def write(rel: str, files: dict[str, str]) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)


def answer(rows: list[dict[str, str]], keep) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    picked = [r for r in rows if keep(r)]
    venue = Counter(r["venue"] for r in picked).most_common(1)[0][0]
    reason = Counter(r.get("reason", "") for r in picked if r["venue"] == venue).most_common(1)
    return venue, reason[0][0]


def incident(venue: str, reason: str) -> str:
    return json.dumps({"venue": venue, "reason_code": reason}, indent=2) + "\n"


def main() -> None:
    rows = [
        dict(kv.split("=", 1) for kv in line.split()[1:]) | {"ts": line.split()[0]}
        for line in setup.lines()
    ]
    day = lambda r: r["ts"].startswith("2024-03-15")  # noqa: E731
    correct = answer(rows, lambda r: r["status"] == "REJECTED" and day(r))
    assert correct == setup.truth(), (correct, setup.truth())
    whole_file = answer(rows, lambda r: r["status"] == "REJECTED")
    grep = answer(rows, lambda r: "REJECTED" in r["status"] and day(r))
    arcx_all = Counter(
        r["reason"] for r in rows if r["status"] == "REJECTED" and r["venue"] == correct[0]
    ).most_common(1)[0][0]
    reason_whole_file = (correct[0], arcx_all)
    traps = {correct, whole_file, grep, reason_whole_file}
    assert len(traps) == 4, f"each trap must give a different answer: {traps}"

    write("oracle", {"incident.json": incident(*correct)})
    write("wrong/whole-file", {"incident.json": incident(*whole_file)})
    write("wrong/grep-counts-cancel-rejects", {"incident.json": incident(*grep)})
    write("wrong/reason-from-whole-file", {"incident.json": incident(*reason_whole_file)})
    reread = [
        {"tool_calls": [["bash", {"command": "grep REJECTED logs/oms.log | wc -l"}]]},
        {"tool_calls": [["bash", {"command": "grep 2024-03-15 logs/oms.log | head"}]]},
    ]
    write(
        "wrong/reread-in-run-2",
        {"incident.json": incident(*correct), "_runs.json": json.dumps(reread, indent=1)},
    )
    early = [{"changed": {"incident.json": "added"}}, {"changed": {}}]
    write(
        "wrong/wrote-in-run-1",
        {"incident.json": incident(*correct), "_runs.json": json.dumps(early, indent=1)},
    )
    v, r = correct
    write(
        "alt/lowercase-extra-fields",
        {
            "incident.json": json.dumps(
                {"venue": v.lower(), "reason_code": r.lower(), "rejects": 410}
            )
        },
    )


if __name__ == "__main__":
    main()
