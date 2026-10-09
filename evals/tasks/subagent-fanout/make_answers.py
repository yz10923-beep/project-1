"""Regenerates oracle/, wrong/ and alt/. Each wrong/ answer is what a naive reading of
the generated text yields, computed here from the text (not assumed), which also proves
each trap changes the answer.

    uv run python evals/tasks/subagent-fanout/make_answers.py
"""

import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root, for `evals`
from evals.harness import load_task_module  # noqa: E402

HERE = Path(__file__).parent
setup = load_task_module(HERE, "setup")


def write(rel: str, answer: dict[str, Any], extra: dict[str, str] | None = None) -> None:
    d = HERE / rel
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "incidents.json").write_text(json.dumps(answer, indent=1) + "\n")
    for name, text in (extra or {}).items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)


def burst(stamps: list[tuple[datetime, str]]) -> tuple[datetime, str] | None:
    """The goal's rule over (time, code) pairs."""
    for i, (t, code) in enumerate(stamps):
        if sum(t <= u <= t + timedelta(seconds=60) for u, _ in stamps[i : i + 400]) >= 20:
            return t, code
    return None


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    right = setup.truth()
    texts = setup.texts()

    # grep ERROR in the Java logs: every stack trace adds two more ERROR lines
    java_ts = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ERROR .* - (\S+)")
    grep_extra = {}
    for svc in ("settlement", "reporting"):
        stamps: list[tuple[datetime, str]] = []
        last: tuple[datetime, str] | None = None
        for line in texts[svc].splitlines():
            if m := java_ts.match(line):
                last = (datetime.fromisoformat(f"{m[1]}.{m[2]}").replace(tzinfo=UTC), m[3])
            if "ERROR" in line and last is not None:
                stamps.append(last)
        if (b := burst(stamps)) is not None:
            grep_extra[svc] = {"start": iso(b[0]), "code": b[1]}
    assert list(grep_extra) == ["settlement"], grep_extra

    # case-insensitive "error" also catches reporting's WARN burst
    warn_stamps = [
        (datetime.fromisoformat(f"{m[1]}.{m[2]}").replace(tzinfo=UTC), "RP-EXPORT")
        for line in texts["reporting"].splitlines()
        if "error" in line.lower() and (m := re.match(r"^(\S+ \S+),(\d{3})", line))
    ]
    warn = burst(warn_stamps)
    assert warn is not None

    # position-keeper's local time read as UTC
    pk = right["position-keeper"]
    local = datetime.fromisoformat(pk["start"].replace("Z", "+00:00")) + timedelta(
        hours=5, minutes=30
    )

    near = setup.NEAR_MISS
    first_error = {s: next(e for e in setup.records()[s] if e.level == "ERROR") for s in right}
    answers = {
        "wrong/stack-traces-counted": right | grep_extra,
        "wrong/warn-counted": right | {"reporting": {"start": iso(warn[0]), "code": "RP-EXPORT"}},
        "wrong/local-time": right | {"position-keeper": {**pk, "start": iso(local)}},
        "wrong/near-miss": right | {"risk-engine": {"start": iso(near[1]), "code": near[2]}},
        "wrong/missed-syslog": {k: v for k, v in right.items() if k != "market-data"},
        "wrong/first-error-of-day": {
            s: {"start": iso(e.ts), "code": e.code} for s, e in first_error.items()
        },
    }
    write("oracle", right)
    for rel, answer in answers.items():
        assert answer != right, rel
        write(rel, answer)
    write("wrong/tampered-logs", right, {"logs/pricing.log": "rotated\n"})
    write(
        "alt/offsets-and-millis",
        {
            s.upper(): {
                "start": v["start"].replace("Z", ".000+00:00").replace("T", " "),
                "code": v["code"].lower(),
            }
            for s, v in right.items()
        },
    )


if __name__ == "__main__":
    main()
