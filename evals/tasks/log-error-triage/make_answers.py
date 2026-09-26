"""Regenerates oracle/, wrong/ and alt/ from the same log the agent sees.

Each wrong/ answer is what a specific naive approach actually produces on this log,
computed by parsing the rendered file (the way an agent would), not hand-written. If
setup.py changes, re-run this, then `make evals-selftest`:

    uv run python evals/tasks/log-error-triage/make_answers.py
"""

import json
import re
import shutil
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.harness import load_task_module  # noqa: E402

HERE = Path(__file__).parent
setup = load_task_module(HERE, "setup")
LINE = re.compile(r'^(\S+) host=(\S+) level=(\S+) service=(\S+) msg="(.*)"$')
S, E = "2024-03-15T09:00:00.000Z", "2024-03-15T10:00:00.000Z"
Row = tuple[str, ...]


def answer(rows: list[Row], keep: Callable[[Row], bool], by_time: bool = True) -> dict[str, object]:
    errs = [r for r in rows if keep(r)]
    service, count = Counter(r[3] for r in errs).most_common(1)[0]
    mine = [r for r in errs if r[3] == service]
    first = min(mine, key=lambda r: r[0]) if by_time else mine[0]
    return {"service": service, "count": count, "first_error": first[4]}


def write(rel: str, files: dict[str, str]) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)


def main() -> None:
    text = setup.render(setup.records())
    rows = [m.groups() for line in text.splitlines() if (m := LINE.match(line))]
    in_win = lambda r: S <= r[0] < E  # noqa: E731
    correct = answer(rows, lambda r: r[2] == "ERROR" and in_win(r))
    assert correct == setup.truth(setup.records()), "file parsing disagrees with truth()"

    def js(a: dict[str, object]) -> str:
        return json.dumps(a, indent=2) + "\n"

    write("oracle", {"answer.json": js(correct)})
    wrong = {
        "whole-file": answer(rows, lambda r: r[2] == "ERROR"),
        "inclusive-end": answer(rows, lambda r: r[2] == "ERROR" and S <= r[0] <= E),
        "exclusive-start": answer(rows, lambda r: r[2] == "ERROR" and S < r[0] < E),
        "grep-i-error": answer(rows, lambda r: "error" in " ".join(r).lower() and in_win(r)),
        "first-in-file": answer(rows, lambda r: r[2] == "ERROR" and in_win(r), by_time=False),
    }
    shutil.rmtree(HERE / "wrong", ignore_errors=True)
    for name, a in wrong.items():
        assert a != correct, f"trap {name} no longer discriminates"
        write(f"wrong/{name}", {"answer.json": js(a)})
    # Right answer, but the agent "cleaned up" the evidence.
    only_errors = "".join(line + "\n" for line in text.splitlines() if "level=ERROR" in line)
    write("wrong/tampered-log", {"answer.json": js(correct), "logs/app.log": only_errors})
    # Different formatting of the right answer: must still pass (grader not too rigid).
    shutil.rmtree(HERE / "alt", ignore_errors=True)
    loose = {
        "service": f"  {str(correct['service']).upper()} ",
        "count": str(correct["count"]),
        "first_error": f'"{correct["first_error"]}"',
    }
    write("alt/loose-formatting", {"answer.json": json.dumps(loose) + "\n"})
    print(f"wrote oracle, {len(wrong) + 1} wrong, 1 alt; truth = {correct}")


if __name__ == "__main__":
    main()
