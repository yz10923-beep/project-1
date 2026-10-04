"""Regenerates oracle/, wrong/ and alt/: each wrong/ is a specific way to lose or
misread a fact across the session. Re-run after editing setup.py:

    uv run python evals/tasks/long-session-recall/make_answers.py
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.harness import load_task_module  # noqa: E402

HERE = Path(__file__).parent
setup = load_task_module(HERE, "setup")
WATCH = (HERE / "fixture" / "watchlist.txt").read_text()


def write(rel: str, files: dict[str, str]) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    for name, text in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)


def note(account: str, usd: object, breaches: dict[str, object]) -> str:
    return json.dumps({"largest_exposure": {"account": account, "usd": usd}, "breaches": breaches})


def main() -> None:
    t = setup.truth()
    acct, usd = t["largest_exposure"]["account"], t["largest_exposure"]["usd"]
    good = {
        "morning_note.json": note(acct, usd, t["breaches"]),
        "watchlist.txt": WATCH + t["watch"] + "\n",
    }
    eod = setup._gross(setup.positions()[1])
    eod_top = max(eod, key=lambda a: eod[a])
    at_limit = {f: len(setup._breaches(setup.trades()[f], strict=False)) for f in setup.FILES}
    assert eod_top != acct and at_limit != t["breaches"]

    write("oracle", good)
    write(
        "wrong/reread-end-of-day",
        {**good, "morning_note.json": note(eod_top, round(eod[eod_top]), t["breaches"])},
    )
    write(
        "wrong/at-limit-counts-as-breach", {**good, "morning_note.json": note(acct, usd, at_limit)}
    )
    write("wrong/watchlist-change-lost", {"morning_note.json": good["morning_note.json"]})
    write("wrong/watchlist-overwritten", {**good, "watchlist.txt": t["watch"] + "\n"})
    early = [{"changed": {"watchlist.txt": "modified"}}, {}, {}, {}, {}]
    write("wrong/wrote-in-run-1", {**good, "_runs.json": json.dumps(early)})
    overflow = [{}, {}, {"status": "context_overflow"}]
    write("wrong/overflowed", {**good, "_runs.json": json.dumps(overflow)})
    write(
        "alt/strings-and-extra-fields",
        {
            **good,
            "morning_note.json": json.dumps(
                {
                    "largest_exposure": {"account": acct.lower(), "usd": f"${usd:,.2f}"},
                    "breaches": {f: str(n) for f, n in t["breaches"].items()},
                    "source": "morning positions check",
                },
                indent=2,
            ),
        },
    )


if __name__ == "__main__":
    main()
