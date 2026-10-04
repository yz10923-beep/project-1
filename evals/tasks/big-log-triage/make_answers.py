"""Regenerates oracle/, wrong/ and alt/. Each wrong/ answer is what a specific naive
approach yields on the generated log. Re-run after editing setup.py:

    uv run python evals/tasks/big-log-triage/make_answers.py
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


def incident(start: str, dependency: str, rejected: int) -> str:
    return json.dumps({"start": start, "dependency": dependency, "rejected": rejected}) + "\n"


def main() -> None:
    lines = setup.text().splitlines()
    ts = lambda line: line.split(" ", 1)[0]  # noqa: E731
    want = setup.truth()
    start, dep, n = str(want["start"]), str(want["dependency"]), int(want["rejected"])  # type: ignore[call-overload]

    prefix = [line for line in lines if "reason=MARGIN_TIMEOUT" in line]  # grep, no anchor
    exact = [line for line in lines if line.endswith("reason=MARGIN_TIMEOUT")]
    assert len(exact) == n and len(prefix) > n
    first_in_file = ts(exact[0])
    prefix_start = min(ts(line) for line in prefix)
    day_dep = Counter(
        line.split(" dep=", 1)[1].split()[0] for line in lines if 'msg="upstream timeout"' in line
    ).most_common(1)[0][0]
    traps = {
        "start": {start, first_in_file, prefix_start},
        "dependency": {dep, day_dep},
        "rejected": {n, len(prefix)},
    }
    for k, v in traps.items():
        assert len(v) == {"start": 3, "dependency": 2, "rejected": 2}[k], (k, v)

    write("oracle", {"incident.json": incident(start, dep, n)})
    write("wrong/first-line-in-file", {"incident.json": incident(first_in_file, dep, n)})
    write("wrong/grep-prefix", {"incident.json": incident(prefix_start, dep, len(prefix))})
    write("wrong/whole-day-dependency", {"incident.json": incident(start, day_dep, n)})
    write(
        "wrong/tampered-log",
        {"incident.json": incident(start, dep, n), setup.LOG_PATH: "truncated\n"},
    )
    overflow = [{"status": "context_overflow", "changed": {"incident.json": "added"}}]
    write(
        "wrong/overflowed",
        {"incident.json": incident(start, dep, n), "_runs.json": json.dumps(overflow)},
    )
    write(
        "alt/loose-formatting",
        {
            "incident.json": json.dumps(
                {
                    "start": start.replace("T", " ").removesuffix("Z"),
                    "dependency": f" {dep.upper()} ",
                    "rejected": str(n),
                    "note": "window from first to last MARGIN_TIMEOUT reject",
                },
                indent=2,
            )
        },
    )


if __name__ == "__main__":
    main()
