"""Eval harness for `kama run`.

One *task* = goal + fixture workspace + hidden checker. One *trial* = one run of the
real agent (`run_goal`, the same entry point the CLI uses) on a fresh copy of the
fixture, graded by what it left on disk, not by what it said.

Output (per variant, e.g. evals/results/kama-run/baseline/):
  results.jsonl          one row per scored (task, rep); resume skips rows already here
  errors.jsonl           attempts that never produced a scorable result (API error,
                         timeout, grader crash, wrong model), kept out of the scores
  traces/<id>_rep<k>.json  readable transcript per trial
  events/<id>_rep<k>_a<n>/ raw events.jsonl per attempt
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import importlib.util
import json
import math
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kama_claude.core.agent.loop import RunResult
from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.runner import make_provider, run_goal
from kama_claude.core.bus.events import (
    EVENT_ADAPTER,
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    Event,
    LLMResponseEvent,
    LLMRetryEvent,
    NoteUpdatedEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolFinishedEvent,
    ToolPolicyEvent,
    ToolStartedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.pricing import cost_usd
from kama_claude.core.llm.types import LLMProvider, ToolCall
from kama_claude.core.plan import PLAN_TOOL_NAMES
from kama_claude.core.policy.classify import analyze_bash
from kama_claude.core.policy.paths import PathContext
from kama_claude.core.session import SessionStore

FLOW = "kama-run"
EVALS_DIR = Path(__file__).resolve().parent
TASKS_DIR = EVALS_DIR / "tasks"
RESULTS_DIR = EVALS_DIR / "results" / FLOW
APPROVAL_FILE = EVALS_DIR / "harness.sha256"

# Files that are build noise, not agent output.
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".kama"}

# Cost: kama_claude.core.llm.pricing.cost_usd (one price table for the agent and evals).


# ---------------------------------------------------------------- tasks & checks


@dataclass(frozen=True)
class RunSpec:
    """One goal of a task. Multi-run tasks (S4) send several goals to the same workspace,
    continuing the session or starting a new one, to test what the agent remembers."""

    goal: str
    new_session: bool = False  # False: continue the previous run's session


@dataclass(frozen=True)
class Task:
    id: str
    goal: str  # the first run's goal
    tags: list[str]
    dir: Path
    max_steps: int | None = None
    oracle_reply: str = "Done."
    runs: tuple[RunSpec, ...] = ()
    # S6: a task can set a low token budget so compaction happens at eval size
    context_budget: int | None = None

    @property
    def fixture(self) -> Path:
        return self.dir / "fixture"

    @property
    def run_specs(self) -> tuple[RunSpec, ...]:
        return self.runs or (RunSpec(self.goal),)


@dataclass(frozen=True)
class RunRecord:
    """What one run of a multi-run task did, for checks on the trajectory (e.g. "run 2
    used what run 1 learned without re-reading the log")."""

    goal: str
    status: str
    final_text: str
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()
    changed: dict[str, str] = field(default_factory=dict)  # files this run changed
    new_session: bool = False
    # S5: indexes into tool_calls that did not run (blocked by policy or denied), and
    # each policy block as (rule, kind, repeated)
    denied: frozenset[int] = frozenset()
    blocks: tuple[tuple[str, str, bool], ...] = ()

    def touched(self, needle: str) -> bool:
        """Did any tool call mention `needle` (a path read, a command run)?"""
        return any(needle in json.dumps(inp) for _, inp in self.tool_calls)

    def commands(self) -> list[str]:
        return [str(inp.get("command", "")) for name, inp in self.tool_calls if name == "bash"]

    def executed_commands(self) -> list[str]:
        """bash commands that actually ran (not blocked or denied)."""
        return [
            str(inp.get("command", ""))
            for i, (name, inp) in enumerate(self.tool_calls)
            if name == "bash" and i not in self.denied
        ]

    def network_commands(self) -> list[str]:
        """Commands that ran and that the policy's classifier says use the network:
        judged the same way whether the policy was on or off for this trial."""
        ctx = PathContext.for_workspace(Path.cwd())
        return [c for c in self.executed_commands() if analyze_bash(c, ctx).network]


@dataclass(frozen=True)
class Outcome:
    """What a checker may look at besides the workspace itself."""

    status: str
    final_text: str
    changed: dict[str, str] = field(default_factory=dict)  # relpath -> added|modified|deleted
    runs: tuple[RunRecord, ...] = ()  # one per goal, in order (multi-run tasks)


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reason: str


def load_tasks(ids: Iterable[str] | None = None) -> list[Task]:
    wanted = set(ids) if ids else None
    tasks = []
    for toml_path in sorted(TASKS_DIR.glob("*/task.toml")):
        d = toml_path.parent
        if wanted is not None and d.name not in wanted:
            continue
        cfg = tomllib.loads(toml_path.read_text())
        runs = tuple(
            RunSpec(r["goal"].strip(), r.get("session", "same") == "new")
            for r in cfg.get("runs", [])
        )
        tasks.append(
            Task(
                id=d.name,
                goal=runs[0].goal if runs else cfg["goal"].strip(),
                tags=list(cfg.get("tags", [])),
                dir=d,
                max_steps=cfg.get("max_steps"),
                oracle_reply=cfg.get("oracle_reply", "Done."),
                runs=runs,
                context_budget=cfg.get("context_budget"),
            )
        )
    if wanted is not None and (missing := wanted - {t.id for t in tasks}):
        raise SystemExit(f"unknown task(s): {sorted(missing)}")
    return tasks


def run_check(task: Task, ws: Path, outcome: Outcome) -> CheckResult:
    """Import the task's check.py and grade. Exceptions propagate (= grader bug)."""
    module = load_task_module(task.dir, "check")
    try:
        passed, reason = module.check(ws, outcome, task.fixture)
    except subprocess.TimeoutExpired:
        return CheckResult(False, "agent's code timed out under the checker")
    return CheckResult(bool(passed), str(reason))


def run_py(ws: Path, *args: str, stdin: str | None = None, timeout: float = 30) -> Any:
    """Run agent-written Python in the workspace. For use by check.py files."""
    return subprocess.run(
        [sys.executable, *args],
        cwd=ws,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not _IGNORED_PARTS.intersection(rel.parts):
            out[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def diff_snapshots(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    changed = {p: "added" for p in after.keys() - before.keys()}
    changed |= {p: "deleted" for p in before.keys() - after.keys()}
    changed |= {p: "modified" for p in before.keys() & after.keys() if before[p] != after[p]}
    return dict(sorted(changed.items()))


@functools.cache
def load_task_module(task_dir: Path, name: str) -> Any:
    """Import `<task_dir>/<name>.py` once per process (task dirs contain hyphens, so no
    normal import). Cached so a task's expensive generated inputs are built only once."""
    path = task_dir / f"{name}.py"
    mod_name = f"evaltask_{task_dir.name.replace('-', '_')}_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses and pickling look modules up by name.
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


DELETE_MANIFEST = "_delete.txt"
SYNTHETIC_RUNS = "_runs.json"  # selftest only: the trajectory a solution stands for


def apply_overlay(ws: Path, overlay: Path) -> None:
    """Copy an oracle/wrong/alt solution onto a workspace. A `_delete.txt` in the overlay
    lists glob patterns (one per line, relative to the workspace) to remove, so a
    solution can express deletions as well as edits."""
    shutil.copytree(
        overlay,
        ws,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(DELETE_MANIFEST, SYNTHETIC_RUNS),
    )
    manifest = overlay / DELETE_MANIFEST
    if not manifest.is_file():
        return
    for pattern in manifest.read_text().split():
        for path in sorted(ws.glob(pattern), reverse=True):  # children before parents
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()


def fresh_workspace(task: Task, parent: Path, overlay: Path | None = None) -> Path:
    """Copy fixture/, then run the task's optional setup.py, which generates inputs too
    big to commit (seeded, so every trial sees identical bytes)."""
    ws = parent / "ws"
    if task.fixture.is_dir():
        shutil.copytree(task.fixture, ws)
    else:
        ws.mkdir()
    for keep in ws.rglob(".gitkeep"):
        keep.unlink()  # placeholder so git tracks empty fixtures; not part of the task
    if (task.dir / "setup.py").is_file():
        load_task_module(task.dir, "setup").setup(ws)
    if overlay is not None:
        apply_overlay(ws, overlay)
    return ws


def between_runs(task: Task, ws: Path, finished: int) -> None:
    """The world moving on between runs (e.g. a rate file refreshed): setup.py's optional
    `between(ws, finished)` hook, called after run `finished` (1-based) of a multi-run task."""
    if (task.dir / "setup.py").is_file():
        hook = getattr(load_task_module(task.dir, "setup"), "between", None)
        if hook is not None:
            hook(ws, finished)


def synthetic_runs(
    task: Task, overlay: Path | None, changed: dict[str, str], reply: str
) -> tuple[RunRecord, ...]:
    """Selftest stand-in for real runs: an overlay's `_runs.json` lists, per run,
    {"tool_calls": [[name, input], ...], "final_text": ..., "changed": {...}, "denied":
    [indexes of calls that didn't run], "blocks": [[rule, kind, repeated], ...], "status":
    "completed" | "context_overflow" | ...}. Without one,
    the runs made no tool calls and the last one made all the changes."""
    specs = task.run_specs
    raw: list[dict[str, Any]] = []
    if overlay is not None and (overlay / SYNTHETIC_RUNS).is_file():
        raw = json.loads((overlay / SYNTHETIC_RUNS).read_text())
    records = []
    for i, spec in enumerate(specs):
        r = raw[i] if i < len(raw) else {}
        last = i == len(specs) - 1
        records.append(
            RunRecord(
                goal=spec.goal,
                status=r.get("status", "completed"),
                final_text=r.get("final_text", reply if last else ""),
                tool_calls=tuple((n, inp) for n, inp in r.get("tool_calls", [])),
                changed=r.get("changed", changed if last else {}),
                new_session=spec.new_session,
                denied=frozenset(r.get("denied", [])),
                blocks=tuple((rule, kind, rep) for rule, kind, rep in r.get("blocks", [])),
            )
        )
    return tuple(records)


# ---------------------------------------------------------------- self-test


def selftest(tasks: list[Task]) -> list[str]:
    """Grade known-good and known-bad end states without calling any model.

    oracle/ (a correct solution) and every alt/<name>/ (a correct solution formatted
    differently, proving the grader isn't too rigid) must pass; the untouched fixture
    (null) and every wrong/<name>/ (plausible-but-wrong or cheating solution) must fail.
    Returns problems.
    """
    problems: list[str] = []

    def grade(task: Task, overlay: Path | None, reply: str) -> CheckResult:
        with tempfile.TemporaryDirectory() as tmp:
            ws = fresh_workspace(task, Path(tmp))
            before = snapshot(ws)
            for finished in range(1, len(task.run_specs)):
                between_runs(task, ws, finished)
            if overlay is not None:
                apply_overlay(ws, overlay)
            changed = diff_snapshots(before, snapshot(ws))
            runs = synthetic_runs(task, overlay, changed, reply)
            return run_check(task, ws, Outcome(runs[-1].status, reply, changed, runs))

    for task in tasks:
        oracle = task.dir / "oracle"
        r = grade(task, oracle if oracle.is_dir() else None, task.oracle_reply)
        if not r.passed:
            problems.append(f"{task.id}: oracle FAILED ({r.reason})")
        r = grade(task, None, "")
        if r.passed:
            problems.append(f"{task.id}: null (did nothing, said nothing) PASSED")
        for wrong in sorted((task.dir / "wrong").glob("*/")):
            r = grade(task, wrong, task.oracle_reply)
            if r.passed:
                problems.append(f"{task.id}: wrong/{wrong.name} PASSED ({r.reason})")
        for alt in sorted((task.dir / "alt").glob("*/")):
            r = grade(task, alt, task.oracle_reply)
            if not r.passed:
                problems.append(f"{task.id}: alt/{alt.name} FAILED ({r.reason})")
    return problems


# ---------------------------------------------------------------- harness integrity


def harness_sha() -> str:
    """Hash of everything that decides a score: harness code, tasks, fixtures, checkers."""
    h = hashlib.sha256()
    files = [EVALS_DIR / "harness.py", EVALS_DIR / "run_evals.py"]
    files += [p for p in sorted(TASKS_DIR.rglob("*")) if p.is_file()]
    for p in files:
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(EVALS_DIR).as_posix().encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


# ---------------------------------------------------------------- running trials


class SuiteAborted(Exception):
    """Something that would repeat on every remaining trial: a request error (bad config,
    unsupported parameter, auth) or trials running under other conditions than the
    variant's. Fix the cause and re-run; scored trials are kept and resumed."""


@dataclass
class RunConfig:
    settings: Settings
    variant: str = "baseline"
    reps: int = 3
    concurrency: int = 2
    timeout_s: float = 900
    max_attempts: int = 3  # only serving errors are retried
    provider_factory: Callable[[Settings], LLMProvider] | None = None
    results_dir: Path = RESULTS_DIR
    # Set by the first non-retryable request error; stops the remaining trials.
    abort_reason: str | None = None
    # What every trial of this variant ran under; from its scored rows, or the first trial.
    conditions: dict[str, Any] | None = None

    @property
    def variant_dir(self) -> Path:
        return self.results_dir / self.variant


async def _allow_all(_: ToolCall) -> bool:
    return True


def read_events(path: Path) -> list[Event]:
    if not path.is_file():
        return []
    return [EVENT_ADAPTER.validate_json(line) for line in path.read_text().splitlines() if line]


def _usage_sum(events: list[Event]) -> dict[str, int]:
    totals: Counter[str] = Counter()
    for e in events:
        # compaction calls are billed too (S6)
        if isinstance(e, LLMResponseEvent | ContextCompactedEvent | ContextCompactionFailedEvent):
            totals.update(e.usage.model_dump())
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {k: totals[k] for k in keys}


def _planning(events: list[Event]) -> bool:
    return any(isinstance(e, RunStartedEvent) and e.planning for e in events)


def plan_metrics(events: list[Event]) -> dict[str, int] | None:
    """How the agent used its plan; None when the task_* tools were not offered."""
    if not _planning(events):
        return None
    snaps = [e for e in events if isinstance(e, PlanUpdatedEvent)]
    final = snaps[-1].tasks if snaps else []
    completed = sum(t.status == "completed" for t in final)
    cancelled = sum(t.status == "cancelled" for t in final)
    return {
        "tasks": len(final),
        "completed": completed,
        "cancelled": cancelled,
        "open": len(final) - completed - cancelled,
        "updates": len(snaps),
        "task_calls": sum(
            isinstance(e, ToolStartedEvent) and e.name in PLAN_TOOL_NAMES for e in events
        ),
        "reminders": sum(isinstance(e, PlanReminderEvent) for e in events),
        # model calls whose only tool calls were task_*: pure bookkeeping round-trips
        "plan_only_steps": sum(
            isinstance(e, LLMResponseEvent)
            and bool(calls := [b["name"] for b in e.content if b["type"] == "tool_use"])
            and all(n in PLAN_TOOL_NAMES for n in calls)
            for e in events
        ),
        # plan-only steps not counted against max_steps (S3 allowance)
        "budget_credit": sum(e.budget_credit for e in events if isinstance(e, RunFinishedEvent)),
    }


def memory_metrics(events: list[Event]) -> dict[str, int]:
    """What memory did across a trial's runs (rows of memory-off trials hold None)."""
    starts = [e for e in events if isinstance(e, RunStartedEvent)]
    changes = [e for e in events if isinstance(e, NoteUpdatedEvent)]
    return {
        "runs_continuing": sum(e.history_messages > 0 for e in starts),
        "runs_with_notes": sum("\n- [" in (e.preamble or "") for e in starts),
        "notes_saved": sum(e.action == "added" for e in changes),
        "notes_updated": sum(e.action == "updated" for e in changes),
        "notes_deleted": sum(e.action == "deleted" for e in changes),
        "volatile_saved": sum(e.action == "added" and e.note.volatile for e in changes),
    }


def safety_metrics(events: list[Event]) -> dict[str, Any]:
    """What the policy, the sandbox and the retries did across a trial's runs (S5)."""
    blocks = [e for e in events if isinstance(e, ToolPolicyEvent) and e.action == "deny"]
    finished = [e for e in events if isinstance(e, RunFinishedEvent)]
    started = [e for e in events if isinstance(e, RunStartedEvent)]
    errors: Counter[str] = Counter()
    for f in finished:
        errors.update(f.tool_errors)
    policy = next((e.policy for e in started if e.policy), None)
    return {
        "policy": policy is not None,
        "mode": policy["mode"] if policy else None,
        "sandbox": policy["sandbox"] if policy else None,
        "blocked": len(blocks),
        "repeated": sum(e.repeated for e in blocks),
        "network_blocked": sum(e.kind == "network" for e in blocks),
        "blocked_rules": sorted({e.rule for e in blocks}),
        "asked": sum(f.approvals_asked for f in finished),
        "llm_retries": sum(isinstance(e, LLMRetryEvent) for e in events),
        "tool_errors": dict(errors),
    }


def context_metrics(events: list[Event], statuses: list[str]) -> dict[str, Any]:
    """How big the requests got (S6). A request's size is exact from its usage: input
    plus cache reads plus cache writes (input_tokens alone is only the uncached tail)."""
    sizes = [
        e.usage.input_tokens + e.usage.cache_read_input_tokens + e.usage.cache_creation_input_tokens
        for e in events
        if isinstance(e, LLMResponseEvent)
    ]
    finished = [e for e in events if isinstance(e, ToolFinishedEvent)]
    cuts = [e.cut for e in finished if e.cut is not None]
    return {
        "peak": max(sizes, default=0),
        "mean": round(statistics.mean(sizes)) if sizes else 0,
        "calls": len(sizes),
        "overflow": "context_overflow" in statuses,
        "cut_results": len(cuts),
        "chars_cut": sum(c.original_chars - c.kept_chars for c in cuts),
        "read_output_calls": sum(e.name == "read_output" for e in finished),
        "compactions": sum(isinstance(e, ContextCompactedEvent) for e in events),
        "compaction_failures": sum(isinstance(e, ContextCompactionFailedEvent) for e in events),
    }


@functools.cache
def git_info() -> dict[str, Any] | None:
    """Which code a trial ran: the harness hash covers evals/, this covers the agent too.
    (An S5 confirmation run was once made from a branch without the fix.)"""

    def git(*args: str) -> str:
        out = subprocess.run(
            ["git", *args], cwd=EVALS_DIR, capture_output=True, text=True, timeout=10
        )
        out.check_returncode()
        return out.stdout.strip()

    try:
        return {
            "commit": git("rev-parse", "--short=12", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        }
    except (OSError, subprocess.SubprocessError):
        return None


def to_trace(task: Task, ws: Path, events: list[Event]) -> list[dict[str, Any]]:
    """Events -> the role-based transcript format eval viewers render."""
    turns: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(ws, planning=_planning(events))},
    ]
    for e in events:
        if isinstance(e, RunStartedEvent):  # one per run: multi-run tasks have several
            goal = f"{e.preamble}\n\n{e.goal}" if e.preamble else e.goal
            turns.append({"role": "user", "content": goal})
        elif isinstance(e, LLMResponseEvent):
            thinking = "".join(b.get("thinking", "") for b in e.content if b["type"] == "thinking")
            for b in e.content:
                if b["type"] == "text" and b.get("text", "").strip():
                    turns.append({"role": "assistant", "content": b["text"]})
                elif b["type"] == "tool_use":
                    turns.append(
                        {
                            "role": "tool_call",
                            "name": b["name"],
                            "content": json.dumps(b.get("input", {}), indent=2),
                        }
                    )
            if thinking and len(turns) > 2:
                turns[-1]["thinking"] = thinking
        elif isinstance(e, ToolFinishedEvent):
            turns.append({"role": "tool_result", "name": e.name, "content": e.output})
        elif isinstance(e, PlanReminderEvent | PlanNoticeEvent):
            turns.append({"role": "user", "content": e.text})
    return turns


def _leak_suspect(events: list[Event]) -> bool:
    """bash is not confined to the workspace, so the agent *could* read the answer key.
    Flag any trial whose tool inputs mention the eval directory or checker files."""
    needles = (str(EVALS_DIR), "check.py", "/oracle", "evals/tasks")
    for e in events:
        if isinstance(e, ToolStartedEvent):
            blob = json.dumps(e.input)
            if any(n in blob for n in needles):
                return True
    return False


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def done_keys(variant_dir: Path) -> set[tuple[str, int]]:
    path = variant_dir / "results.jsonl"
    if not path.is_file():
        return set()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line]
    return {(r["prompt_id"], r["rep"]) for r in rows}


def conditions(row: dict[str, Any]) -> dict[str, Any]:
    """What a scored trial ran under. A variant is one condition: rows that differ here
    are not comparable (S5's full arm mixed `none` and `bwrap` across a resume)."""
    meta = row.get("meta", {})
    return {
        "harness_sha": meta.get("harness_sha"),
        "model": row.get("model"),
        "effort": meta.get("effort"),
        "memory": meta.get("memory"),
        "policy": meta.get("policy"),
        "context": meta.get("context"),
        "sandbox": (row.get("safety") or {}).get("sandbox"),
    }


def condition_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, list[Any]]:
    return {k: [a.get(k), b.get(k)] for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)}


def _mixed_conditions(rows: list[dict[str, Any]]) -> dict[str, Counter[str]]:
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        for k, v in conditions(r).items():
            seen[k][str(v)[:12] if k == "harness_sha" else str(v)] += 1
    return {k: c for k, c in seen.items() if len(c) > 1}


@dataclass
class _Ran:
    result: RunResult
    run_dir: Path
    events: list[Event]
    record: RunRecord


async def _run_all(task: Task, ws: Path, settings: Settings, provider: LLMProvider) -> list[_Ran]:
    """Run each goal of the task in order. A multi-run task keeps one session going
    (or starts a new one where the task says so) in a per-trial store, so trials never
    share memory. Stops early if a run ends with an API/internal error."""
    multi = len(task.run_specs) > 1
    store = SessionStore(settings.sessions_dir)
    session_id: str | None = None
    out: list[_Ran] = []
    for i, spec in enumerate(task.run_specs):
        if i > 0:
            between_runs(task, ws, i)
        if multi and (session_id is None or spec.new_session):
            session_id = store.create(ws).session_id
        before = snapshot(ws)
        result, run_dir = await run_goal(
            spec.goal,
            settings=settings,
            workspace=ws,
            approver=_allow_all,
            provider=provider,
            session_id=session_id,
            sessions=store,
            mode="auto",  # unattended, like -y: nobody answers an ask, so asks are denies
        )
        events = read_events(run_dir / "events.jsonl")
        started = [e for e in events if isinstance(e, ToolStartedEvent)]
        denied_ids = {
            e.tool_use_id for e in events if isinstance(e, ToolFinishedEvent) and e.denied
        }
        record = RunRecord(
            goal=spec.goal,
            status=result.status,
            final_text=result.final_text,
            tool_calls=tuple((e.name, e.input) for e in started),
            changed=diff_snapshots(before, snapshot(ws)),
            new_session=spec.new_session,
            denied=frozenset(i for i, e in enumerate(started) if e.tool_use_id in denied_ids),
            blocks=tuple(
                (e.rule, e.kind, e.repeated)
                for e in events
                if isinstance(e, ToolPolicyEvent) and e.action == "deny"
            ),
        )
        out.append(_Ran(result, run_dir, events, record))
        if result.status in ("error", "context_overflow"):
            break  # an overflowed session overflows again on its next run
    return out


async def run_trial(task: Task, rep: int, cfg: RunConfig) -> dict[str, Any] | None:
    """Run one (task, rep) until it yields a scorable row. Failed attempts -> errors.jsonl."""
    vdir = cfg.variant_dir
    for attempt in range(1, cfg.max_attempts + 1):
        with tempfile.TemporaryDirectory(prefix=f"kama-eval-{task.id}-") as tmp:
            ws = fresh_workspace(task, Path(tmp))
            before = snapshot(ws)
            events_dir = vdir / "events" / f"{task.id}_rep{rep}_a{attempt}"
            if events_dir.exists():
                shutil.rmtree(events_dir)  # left over from an interrupted earlier run
            overrides: dict[str, Any] = {
                # the graders and answers: no tool call may read them (S5), and bwrap
                # hides them from bash entirely
                "private_paths": ",".join(
                    filter(None, [str(EVALS_DIR.resolve()), cfg.settings.private_paths])
                ),
                "runs_dir": events_dir,
                # per trial: notes and sessions must never leak between trials
                "sessions_dir": events_dir / "sessions",
                "memory_dir": events_dir / "memory",
            }
            if task.max_steps:
                overrides["max_steps"] = task.max_steps
            if task.context_budget and cfg.settings.context:
                overrides["context_budget"] = task.context_budget
            settings = cfg.settings.model_copy(update=overrides)
            provider = (cfg.provider_factory or make_provider)(settings)
            err_base = {"prompt_id": task.id, "rep": rep, "attempt": attempt}

            t0 = time.monotonic()
            try:
                ran = await asyncio.wait_for(
                    _run_all(task, ws, settings, provider), timeout=cfg.timeout_s
                )
            except TimeoutError:
                # A hard ceiling, recorded as a timeout, never as a zero score.
                _append(vdir / "errors.jsonl", {**err_base, "class": "timeout"})
                return None
            wall_s = time.monotonic() - t0
            events = [e for r in ran for e in r.events]
            result = ran[-1].result
            usage = _usage_sum(events)
            llm = [e for e in events if isinstance(e, LLMResponseEvent)]
            served = sorted({e.model for e in llm})

            if any(not m.startswith(settings.model) for m in served):
                _append(
                    vdir / "errors.jsonl",
                    {**err_base, "class": "model_mismatch", "served": served, "usage": usage},
                )
                return None
            if result.status == "error":
                internal = (result.error or "").startswith("internal error")
                if internal:
                    cls = "harness_error"
                elif result.retryable is False:
                    # 400/401/404...: the request itself is wrong (bad config, unsupported
                    # parameter, auth). Every other trial will fail the same way.
                    cls = "request_error"
                else:
                    cls = "serving_error"
                _append(
                    vdir / "errors.jsonl",
                    {**err_base, "class": cls, "error": result.error, "usage": usage},
                )
                if cls == "request_error":
                    cfg.abort_reason = (
                        f"a request error will repeat on every trial: {task.id} rep{rep}: "
                        f"{result.error}"
                    )
                    return None
                if internal or attempt == cfg.max_attempts:
                    return None
                await asyncio.sleep(min(60.0, 2**attempt) * random.uniform(0.5, 1.5))
                continue

            outcome = Outcome(
                result.status,
                result.final_text,
                diff_snapshots(before, snapshot(ws)),
                tuple(r.record for r in ran),
            )
            try:
                check = run_check(task, ws, outcome)
            except Exception as e:
                _append(
                    vdir / "errors.jsonl",
                    {**err_base, "class": "grader_error", "error": f"{type(e).__name__}: {e}"},
                )
                return None

            trace_path = vdir / "traces" / f"{task.id}_rep{rep}.json"
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text(json.dumps(to_trace(task, ws, events), indent=1))
            tools = [e for e in events if isinstance(e, ToolFinishedEvent)]
            row: dict[str, Any] = {
                "prompt_id": task.id,
                "rep": rep,
                "prompt": task.goal,
                "tags": task.tags,
                # truncated rows are shown but kept out of the means
                "status": "truncated" if result.status == "truncated" else "ok",
                "run_status": result.status,
                "stop_reason": llm[-1].stop_reason if llm else None,
                "grade": {
                    "passed": 1.0 if check.passed else 0.0,
                    "refused": 1.0 if result.status == "refused" else 0.0,
                },
                "explanation": {"passed": check.reason},
                "model": served[0] if served else settings.model,
                "usage": usage,
                "steps": sum(r.result.steps for r in ran),
                "tool_calls": len(tools),
                "tool_errors": sum(e.is_error for e in tools),
                "plan": plan_metrics(events),
                "memory": memory_metrics(events) if settings.memory else None,
                "safety": safety_metrics(events),
                "context": context_metrics(events, [r.result.status for r in ran]),
                "latency_s": round(sum(e.latency_ms for e in llm) / 1000, 2),
                "wall_s": round(wall_s, 2),
                "attempts": attempt,
                "meta": {
                    "changed": outcome.changed,
                    "final_text": result.final_text[-2000:],
                    "leak_suspect": _leak_suspect(events),
                    "events": [str(r.run_dir.relative_to(vdir)) for r in ran],
                    "harness_sha": harness_sha(),
                    "effort": getattr(provider, "effort", settings.effort),
                    "memory": settings.memory,
                    "policy": settings.policy,
                    "context": settings.context,
                    "context_budget": settings.context_budget,
                    "git": git_info(),
                },
            }
            if len(task.run_specs) > 1:
                row["runs"] = [
                    {
                        "status": r.result.status,
                        "steps": r.result.steps,
                        "tool_calls": len(r.record.tool_calls),
                        "new_session": r.record.new_session,
                        "usage": _usage_sum(r.events),
                    }
                    for r in ran
                ]
            got = conditions(row)
            if cfg.conditions is None:
                cfg.conditions = got
            elif diff := condition_diff(cfg.conditions, got):
                # Not a score for this variant, and every later trial would differ too.
                _append(
                    vdir / "errors.jsonl",
                    {**err_base, "class": "condition_mismatch", "diff": diff, "usage": usage},
                )
                cfg.abort_reason = (
                    f"{task.id} rep{rep} ran under different conditions than this variant's "
                    f"other trials ({', '.join(f'{k}: {a} -> {b}' for k, (a, b) in diff.items())})"
                    ". Run it as a new variant."
                )
                return None
            return row
    return None


async def run_suite(tasks: list[Task], cfg: RunConfig) -> None:
    vdir = cfg.variant_dir
    vdir.mkdir(parents=True, exist_ok=True)
    _write_state_file(cfg.results_dir)
    done = done_keys(vdir)
    scored, _ = _load_rows(vdir)
    if mixed := _mixed_conditions(scored):
        raise SuiteAborted(
            f"{vdir.name} already mixes conditions ({_render_mixed(mixed)}); start a new variant"
        )
    if scored and cfg.conditions is None:
        cfg.conditions = conditions(scored[0])
    todo = [(t, r) for t in tasks for r in range(cfg.reps) if (t.id, r) not in done]
    if len(done):
        print(f"resuming: {len(done)} trial(s) already scored, {len(todo)} to run")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(task: Task, rep: int) -> None:
        async with sem:
            if cfg.abort_reason:
                return
            row = await run_trial(task, rep, cfg)
        if row is None:
            print(f"  {task.id} rep{rep}: NOT SCORED (see errors.jsonl)")
            return
        _append(vdir / "results.jsonl", row)  # written as each trial completes
        mark = "PASS" if row["grade"]["passed"] else "fail"
        leak = "  [LEAK SUSPECT]" if row["meta"]["leak_suspect"] else ""
        print(f"  {task.id} rep{rep}: {mark} · {row['steps']} steps · {row['wall_s']}s{leak}")

    await asyncio.gather(*(one(t, r) for t, r in todo))
    if cfg.abort_reason:
        raise SuiteAborted(cfg.abort_reason)


def _write_state_file(results_dir: Path) -> None:
    """Metric declarations for eval report viewers (first binary metric = headline)."""
    state = {
        "metrics": [
            {"id": "passed", "label": "passed", "kind": "binary"},
            {"id": "refused", "label": "refused", "kind": "binary", "better": "lower"},
        ],
        "perf_fields": [
            {"id": "steps", "label": "steps"},
            {"id": "tool_errors", "label": "tool errors"},
            {"id": "latency_s", "label": "LLM time", "unit": "s"},
            {"id": "wall_s", "label": "wall", "unit": "s"},
        ],
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "_state.json").write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------- summary


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _planning_line(rows: list[dict[str, Any]]) -> str:
    """Did the agent plan, finish its plan, and did the end-of-turn reminder help?"""
    plans = [r["plan"] for r in rows]
    made = [p for p in plans if p["tasks"]]
    reminded = [r for r in rows if r["plan"]["reminders"]]
    rescued = sum(int(r["grade"]["passed"]) for r in reminded)
    task_calls = sum(p["task_calls"] for p in plans)
    tool_calls = sum(r["tool_calls"] for r in rows) or 1
    median_tasks = statistics.median(p["tasks"] for p in made) if made else 0
    return (
        f"- planning: plan made in {len(made)}/{len(rows)} trials "
        f"(median {median_tasks} tasks) · open tasks at end in "
        f"{sum(p['open'] > 0 for p in plans)} · reminded in {len(reminded)} "
        f"(passed after reminder: {rescued}/{len(reminded)}) · task_* = "
        f"{task_calls / tool_calls:.0%} of tool calls · plan-only steps "
        f"{sum(p['plan_only_steps'] for p in plans)}/{sum(r['steps'] for r in rows)} "
        f"({sum(p.get('budget_credit', 0) for p in plans)} not counted against max_steps)"
    )


def _render_mixed(mixed: dict[str, Counter[str]]) -> str:
    return "; ".join(
        f"{k}: " + ", ".join(f"{v} {n}" for v, n in c.most_common()) for k, c in mixed.items()
    )


def _load_rows(variant_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def read(path: Path) -> list[dict[str, Any]]:
        return [json.loads(x) for x in path.read_text().splitlines() if x] if path.exists() else []

    return read(variant_dir / "results.jsonl"), read(variant_dir / "errors.jsonl")


_PART = re.compile(r"(?:^|, )(\w+) (ok|WRONG)\b")


def check_parts(reason: str) -> dict[str, bool]:
    """The named sub-checks in a grader's reason ("answer ok, run2_no_reread WRONG · ..."),
    in order. A pass/fail score hides which part failed; across reps the parts show it."""
    head = reason.split(" · ")[0]
    return {m.group(1): m.group(2) == "ok" for m in _PART.finditer(head)}


def part_rates(rows: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Per sub-check: (passed, graded) over the given trials of one task."""
    rates: dict[str, tuple[int, int]] = {}
    for r in rows:
        for name, ok in check_parts(r["explanation"]["passed"]).items():
            k, n = rates.get(name, (0, 0))
            rates[name] = (k + ok, n + 1)
    return rates


def _render_parts(rates: dict[str, tuple[int, int]]) -> str:
    return " · ".join(f"{name} {k}/{n}" for name, (k, n) in rates.items())


def _safety_line(rows: list[dict[str, Any]]) -> str:
    sf = [r["safety"] for r in rows]
    errors: Counter[str] = Counter()
    for x in sf:
        errors.update(x["tool_errors"])
    modes = sorted({f"{x['mode']}/{x['sandbox']}" for x in sf if x["policy"]}) or ["policy off"]
    blocked = [x for x in sf if x["blocked"]]
    rules = Counter(rule for x in sf for rule in x["blocked_rules"])
    top = ", ".join(f"{rule} {n}" for rule, n in rules.most_common(4))
    return (
        f"- safety ({', '.join(modes)}): blocked calls in {len(blocked)}/{len(sf)} trials "
        f"({sum(x['blocked'] for x in sf)} blocks, {sum(x['repeated'] for x in sf)} repeated, "
        f"{sum(x['network_blocked'] for x in sf)} network){f' [{top}]' if top else ''} · "
        f"model retries {sum(x['llm_retries'] for x in sf)} · tool errors "
        + (", ".join(f"{k} {v}" for k, v in errors.most_common()) or "none")
    )


def summarize(variant_dir: Path) -> str:
    rows, errs = _load_rows(variant_dir)
    ok = [r for r in rows if r["status"] == "ok"]
    if not ok:
        return f"no scored trials in {variant_dir}"

    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ok:
        by_task[r["prompt_id"]].append(r)
    k = sum(int(r["grade"]["passed"]) for r in ok)
    n = len(ok)
    lo, hi = wilson(k, n)
    any_pass = sum(any(r["grade"]["passed"] for r in rs) for rs in by_task.values())
    all_pass = sum(all(r["grade"]["passed"] for r in rs) for rs in by_task.values())
    costs = [cost_usd(r["model"], r["usage"]) for r in ok]
    known = [c for c in costs if c is not None]
    err_costs = [cost_usd(e.get("model", ""), e["usage"]) or 0.0 for e in errs if "usage" in e]

    lines = [
        f"## {variant_dir.name}: {k}/{n} trials passed = {k / n:.0%} "
        f"(95% CI {lo:.0%}-{hi:.0%}) over {len(by_task)} tasks",
        f"- pass@k (any rep passed): {any_pass}/{len(by_task)} tasks · "
        f"pass^k (every rep passed): {all_pass}/{len(by_task)} tasks",
        f"- noise floor ≈ ±{1 / math.sqrt(n):.0%}: smaller differences between variants "
        "are not real",
        f"- per trial: median {statistics.median(r['steps'] for r in ok)} steps, "
        f"{statistics.median(r['wall_s'] for r in ok):.0f}s wall, "
        f"{statistics.mean(r['tool_errors'] for r in ok):.1f} tool errors (mean)",
    ]
    if known:
        lines.append(
            f"- cost: ${sum(known):.2f} scored + ${sum(err_costs):.2f} on failed attempts "
            f"(median ${statistics.median(known):.3f}/trial)"
            + (
                f"; {len(costs) - len(known)} rows with unknown model price"
                if len(known) < n
                else ""
            )
        )
    if sized := [r["context"] for r in ok if r.get("context")]:
        overflowed = sum(c["overflow"] for c in sized)
        lines.append(
            f"- context: peak request median "
            f"{statistics.median(c['peak'] for c in sized) / 1000:.1f}K, "
            f"max {max(c['peak'] for c in sized) / 1000:.1f}K tokens"
            f" · overflowed in {overflowed}/{len(sized)} trials"
            f" · results cut {sum(c.get('cut_results', 0) for c in sized)}"
            f", read_output calls {sum(c.get('read_output_calls', 0) for c in sized)}"
        )
        # Every trial is scored. Trials that compacted are reported beside the rest, not
        # instead of them: dropping the ones that didn't would score the on arm on its
        # hardest trials only, against every trial of the off arm.
        compacted = [r for r in ok if (r.get("context") or {}).get("compactions")]
        if compacted or any(c.get("compaction_failures") for c in sized):
            n_comp = sum(r["context"]["compactions"] for r in compacted)
            failed = sum(c.get("compaction_failures", 0) for c in sized)
            n_pass = sum(int(r["grade"]["passed"]) for r in compacted)
            lines.append(
                f"- compaction: compacted in {len(compacted)}/{len(sized)} trials "
                f"({n_comp} compactions, {failed} failed) · passed when compacted "
                f"{n_pass}/{len(compacted)}"
            )
    if mixed := _mixed_conditions(rows):
        lines.append(f"- MIXED CONDITIONS, not one variant: {_render_mixed(mixed)}")
    truncated = len(rows) - n
    if truncated or errs:
        classes = Counter(e["class"] for e in errs)
        lines.append(f"- NOT SCORED: {truncated} truncated, errors {dict(classes)}")
    if out_of_steps := [r for r in ok if r.get("run_status") == "max_steps"]:
        tasks = ", ".join(sorted({r["prompt_id"] for r in out_of_steps}))
        lines.append(f"- ended at max_steps: {len(out_of_steps)} trial(s) ({tasks})")
    if remembered := [r for r in ok if r.get("memory")]:
        m = [r["memory"] for r in remembered]
        multi = [r for r in remembered if len(r.get("runs", [])) > 1]
        lines.append(
            f"- memory: notes saved in {sum(x['notes_saved'] > 0 for x in m)}/{len(m)} trials "
            f"({sum(x['notes_saved'] for x in m)} notes, "
            f"{sum(x['volatile_saved'] for x in m)} volatile) · runs that opened with notes: "
            f"{sum(x['runs_with_notes'] for x in m)} · multi-run trials passed: "
            f"{sum(int(r['grade']['passed']) for r in multi)}/{len(multi)}"
        )
    if planned := [r for r in ok if r.get("plan") is not None]:
        lines.append(_planning_line(planned))
    if guarded := [r for r in ok if r.get("safety")]:
        lines.append(_safety_line(guarded))
    if leaks := [r for r in ok if r["meta"].get("leak_suspect")]:
        lines.append(f"- WARNING: {len(leaks)} trial(s) touched eval files; inspect their traces")
    lines += ["", "| task | tags | passed | steps | reason (last rep) |", "|---|---|---|---|---|"]
    for tid, rs in sorted(by_task.items()):
        rs = sorted(rs, key=lambda r: r["rep"])
        passed = "".join("✓" if r["grade"]["passed"] else "✗" for r in rs)
        steps = ",".join(str(r["steps"]) for r in rs)
        reason = rs[-1]["explanation"]["passed"].replace("|", "/")[:80]
        lines.append(f"| {tid} | {' '.join(rs[0]['tags'])} | {passed} | {steps} | {reason} |")
    parted = {tid: part_rates(rs) for tid, rs in sorted(by_task.items())}
    if any(len(p) > 1 for p in parted.values()):
        lines += ["", "Sub-checks (passed/graded over all reps):"]
        lines += [f"- {tid}: {_render_parts(p)}" for tid, p in parted.items() if len(p) > 1]
    return "\n".join(lines)


def _arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [c for r in rows if (c := cost_usd(r["model"], r["usage"])) is not None]
    return {
        "passed": sum(int(r["grade"]["passed"]) for r in rows),
        "n": len(rows),
        "steps": statistics.median(r["steps"] for r in rows),
        "tools": statistics.median(r["tool_calls"] for r in rows),
        "cost": statistics.median(costs) if costs else None,
        "parts": part_rates(rows),
    }


def compare(a_dir: Path, b_dir: Path) -> str:
    """Two variants side by side, per task: pass counts, each sub-check, median steps,
    tool calls and cost. Only tasks both variants ran are compared."""
    by: list[dict[str, list[dict[str, Any]]]] = []
    for d in (a_dir, b_dir):
        rows, _ = _load_rows(d)
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            if r["status"] == "ok":
                grouped[r["prompt_id"]].append(r)
        by.append(grouped)
    shared = sorted(set(by[0]) & set(by[1]))
    if not shared:
        return f"no task was scored in both {a_dir.name} and {b_dir.name}"
    a, b = a_dir.name, b_dir.name

    def money(x: float | None) -> str:
        return "?" if x is None else f"${x:.3f}"

    lines = [
        f"## {a} vs {b} ({len(shared)} shared tasks)",
        "",
        f"| task | check | {a} | {b} |",
        "|---|---|---|---|",
    ]
    totals = [[0, 0], [0, 0]]
    for tid in shared:
        x, y = _arm(by[0][tid]), _arm(by[1][tid])
        for t, arm in zip(totals, (x, y), strict=True):
            t[0] += arm["passed"]
            t[1] += arm["n"]
        lines.append(f"| {tid} | **passed** | {x['passed']}/{x['n']} | {y['passed']}/{y['n']} |")
        for name in [*x["parts"], *(p for p in y["parts"] if p not in x["parts"])]:
            cells = [
                "/".join(map(str, arm["parts"][name])) if name in arm["parts"] else "-"
                for arm in (x, y)
            ]
            lines.append(f"| | {name} | {cells[0]} | {cells[1]} |")
        lines.append(
            f"| | median steps · tools | {x['steps']} · {x['tools']} | "
            f"{y['steps']} · {y['tools']} |"
        )
        lines.append(f"| | median cost | {money(x['cost'])} | {money(y['cost'])} |")
    (ka, na), (kb, nb) = totals
    lines += [
        "",
        f"Total: {a} {ka}/{na}, {b} {kb}/{nb}. Noise floor ≈ ±{1 / math.sqrt(min(na, nb)):.0%}"
        " of trials: a smaller gap is not evidence of a difference.",
    ]
    return "\n".join(lines)
