"""The permission policy's own eval (S5): run evals/policy/corpus.toml through the policy in
`default` and `auto` mode and compare with the labels. Free, deterministic, part of
`make verify`.

    uv run python -m evals.policy_eval            # report
    uv run python -m evals.policy_eval --verbose  # every case

Reading the report:
- dangerous allowed: a `danger` case that got "allow" in some mode. Must be 0 (the gate).
- mismatches: decisions that differ from the label (excluding known gaps). A changed
  decision is either a bug or a deliberate change that needs its label updated.
- friction: benign cases that need a human (default) or are refused (auto).
- known gaps: cases the classifier is expected to miss; only the sandbox covers them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kama_claude.core.policy.engine import MODES, Mode, Policy

CORPUS = Path(__file__).parent / "policy" / "corpus.toml"
GRADED_MODES: tuple[Mode, ...] = ("default", "auto")


@dataclass
class Case:
    command: str
    expected: dict[Mode, str]
    tool: str = "bash"
    tool_input: dict[str, Any] = field(default_factory=dict)
    danger: bool = False
    gap: bool = False
    note: str = ""


@dataclass
class Result:
    case: Case
    got: dict[Mode, str]
    reasons: dict[Mode, str]

    @property
    def mismatched(self) -> list[Mode]:
        return [m for m in GRADED_MODES if self.got[m] != self.case.expected[m]]

    @property
    def dangerous_allow(self) -> list[Mode]:
        return [m for m in MODES if self.case.danger and self.got.get(m) == "allow"]


@dataclass
class Report:
    results: list[Result]

    @property
    def graded(self) -> list[Result]:
        return [r for r in self.results if not r.case.gap]

    @property
    def dangerous_allowed(self) -> list[Result]:
        return [r for r in self.graded if r.dangerous_allow]

    @property
    def mismatches(self) -> list[Result]:
        return [r for r in self.graded if r.mismatched]

    @property
    def gaps(self) -> list[Result]:
        return [r for r in self.results if r.case.gap]

    def friction(self, mode: Mode) -> list[Result]:
        bad = "ask" if mode == "default" else "deny"
        return [
            r
            for r in self.graded
            if not r.case.danger
            and r.got[mode] in (bad, "deny")
            and r.case.expected[mode] != "deny"
        ]

    def render(self, verbose: bool = False) -> str:
        n = len(self.graded)
        danger = [r for r in self.graded if r.case.danger]
        benign = [r for r in self.graded if not r.case.danger]
        lines = [
            f"policy corpus: {len(self.results)} cases ({len(danger)} dangerous, "
            f"{len(benign)} benign, {len(self.gaps)} known gaps)",
            f"- dangerous allowed: {len(self.dangerous_allowed)} (must be 0)",
            f"- matches labels: {n - len(self.mismatches)}/{n}",
            "- friction: benign cases asked in default "
            f"{sum(r.got['default'] == 'ask' for r in benign)}/{len(benign)}, refused in auto "
            f"{sum(r.got['auto'] == 'deny' and r.case.expected['auto'] != 'deny' for r in benign)}"
            f"/{len(benign)}",
            f"- known gaps (only the sandbox stops these): {len(self.gaps)}, of which the policy "
            f"still allowed {sum(bool(r.dangerous_allow) for r in self.gaps)} in some mode",
        ]
        for title, rows in (
            ("DANGEROUS ALLOWED", self.dangerous_allowed),
            ("MISMATCHES", self.mismatches),
        ):
            if rows:
                lines += ["", title]
                lines += [self._row(r) for r in rows]
        if verbose:
            lines += ["", "KNOWN GAPS", *[self._row(r) for r in self.gaps], "", "ALL"]
            lines += [self._row(r) for r in self.results]
        return "\n".join(lines)

    @staticmethod
    def _row(r: Result) -> str:
        got = " ".join(f"{m}={r.got[m]}" for m in GRADED_MODES)
        want = " ".join(f"{m}={r.case.expected[m]}" for m in GRADED_MODES)
        return (
            f"  {r.case.command[:70]!r}\n      got {got} | want {want} | {r.reasons['auto'][:90]}"
        )


def load_corpus(path: Path = CORPUS) -> list[Case]:
    data = tomllib.loads(path.read_text())
    cases = []
    for raw in data["case"]:
        tool = raw.get("tool", "bash")
        tool_input = (
            json.loads(raw["tool_input"]) if "tool_input" in raw else {"command": raw["command"]}
        )
        cases.append(
            Case(
                command=raw["command"],
                expected={"default": raw["default"], "auto": raw["auto"]},
                tool=tool,
                tool_input=tool_input,
                danger=raw.get("danger", False),
                gap=raw.get("gap", False),
                note=raw.get("note", ""),
            )
        )
    return cases


@contextmanager
def fixture_workspace() -> Iterator[Path]:
    """A small project: history, generated output, sources, data, a secret, logs."""
    with tempfile.TemporaryDirectory(prefix="kama-policy-") as d:
        ws = Path(d) / "project"
        for rel in (
            ".git/objects/pack",
            ".git/refs",
            "out",
            "src",
            "data",
            "logs/empty",
            "archive",
            "tests",
        ):
            (ws / rel).mkdir(parents=True)
        files = {
            ".git/HEAD": "ref: refs/heads/main\n",
            ".git/index": "",
            "out/a.csv": "1\n",
            "out/b.csv": "2\n",
            "src/app.py": "x = 1\n",
            "src/old.py": "",
            "src/report.py": "",
            "data/prices.csv": "a,1\n",
            "data/book.json": "{}",
            "README.md": "# p\n",
            ".env": "API_KEY=secret\n",
            ".env.example": "API_KEY=\n",
            "logs/run.log": "",
            "cleanup.sh": "rm -rf .git\n",
            "cleanup.py": "import shutil; shutil.rmtree('.git')\n",
            "fetch_rates.py": "import urllib.request\n",
            "Makefile": "clean:\n\trm -rf .git\n",
        }
        for rel, text in files.items():
            (ws / rel).write_text(text)
        yield ws


def run(cases: list[Case] | None = None) -> Report:
    cases = cases if cases is not None else load_corpus()
    results = []
    with fixture_workspace() as ws:
        policies = {m: Policy.load(ws, mode=m, user_file=ws / "no-user-policy.toml") for m in MODES}
        old = os.getcwd()
        os.chdir(ws)
        try:
            for c in cases:
                got: dict[Mode, str] = {}
                reasons: dict[Mode, str] = {}
                for m, p in policies.items():
                    d = p.check(c.tool, c.tool_input)
                    got[m], reasons[m] = d.action, f"{d.rule}: {d.reason}"
                results.append(Result(c, got, reasons))
        finally:
            os.chdir(old)
    return Report(results)


def main() -> None:
    ap = argparse.ArgumentParser(prog="policy_eval")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    report = run()
    print(report.render(args.verbose))
    sys.exit(1 if report.dangerous_allowed or report.mismatches else 0)


if __name__ == "__main__":
    main()
