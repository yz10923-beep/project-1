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

from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.runner import make_provider, run_goal
from kama_claude.core.bus.events import (
    EVENT_ADAPTER,
    Event,
    LLMResponseEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMProvider, ToolCall

FLOW = "kama-run"
EVALS_DIR = Path(__file__).resolve().parent
TASKS_DIR = EVALS_DIR / "tasks"
RESULTS_DIR = EVALS_DIR / "results" / FLOW
APPROVAL_FILE = EVALS_DIR / "harness.sha256"

# Files that are build noise, not agent output.
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".kama"}

# $ per million tokens (input, output), first-party API. Cache write = 1.25x input,
# cache read = 0.1x input. Cost is derived at summary time from each row's served
# model, so a model swap can never be priced at a stale rate.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


# ---------------------------------------------------------------- tasks & checks


@dataclass(frozen=True)
class Task:
    id: str
    goal: str
    tags: list[str]
    dir: Path
    max_steps: int | None = None
    oracle_reply: str = "Done."

    @property
    def fixture(self) -> Path:
        return self.dir / "fixture"


@dataclass(frozen=True)
class Outcome:
    """What a checker may look at besides the workspace itself."""

    status: str
    final_text: str
    changed: dict[str, str] = field(default_factory=dict)  # relpath -> added|modified|deleted


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
        tasks.append(
            Task(
                id=d.name,
                goal=cfg["goal"].strip(),
                tags=list(cfg.get("tags", [])),
                dir=d,
                max_steps=cfg.get("max_steps"),
                oracle_reply=cfg.get("oracle_reply", "Done."),
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


def apply_overlay(ws: Path, overlay: Path) -> None:
    """Copy an oracle/wrong/alt solution onto a workspace. A `_delete.txt` in the overlay
    lists glob patterns (one per line, relative to the workspace) to remove, so a
    solution can express deletions as well as edits."""
    shutil.copytree(overlay, ws, dirs_exist_ok=True, ignore=shutil.ignore_patterns(DELETE_MANIFEST))
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
            if overlay is not None:
                apply_overlay(ws, overlay)
            outcome = Outcome("completed", reply, diff_snapshots(before, snapshot(ws)))
            return run_check(task, ws, outcome)

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
        if isinstance(e, LLMResponseEvent):
            totals.update(e.usage.model_dump())
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    return {k: totals[k] for k in keys}


def to_trace(task: Task, ws: Path, events: list[Event]) -> list[dict[str, Any]]:
    """Events -> the role-based transcript format eval viewers render."""
    turns: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt(ws)},
        {"role": "user", "content": task.goal},
    ]
    for e in events:
        if isinstance(e, LLMResponseEvent):
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
            overrides: dict[str, Any] = {"runs_dir": events_dir}
            if task.max_steps:
                overrides["max_steps"] = task.max_steps
            settings = cfg.settings.model_copy(update=overrides)
            provider = (cfg.provider_factory or make_provider)(settings)
            err_base = {"prompt_id": task.id, "rep": rep, "attempt": attempt}

            t0 = time.monotonic()
            try:
                result, run_dir = await asyncio.wait_for(
                    run_goal(
                        task.goal,
                        settings=settings,
                        workspace=ws,
                        approver=_allow_all,
                        provider=provider,
                    ),
                    timeout=cfg.timeout_s,
                )
            except TimeoutError:
                # A hard ceiling, recorded as a timeout, never as a zero score.
                _append(vdir / "errors.jsonl", {**err_base, "class": "timeout"})
                return None
            wall_s = time.monotonic() - t0
            events = read_events(run_dir / "events.jsonl")
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
                cls = "harness_error" if internal else "serving_error"
                _append(
                    vdir / "errors.jsonl",
                    {**err_base, "class": cls, "error": result.error, "usage": usage},
                )
                if internal or attempt == cfg.max_attempts:
                    return None
                await asyncio.sleep(min(60.0, 2**attempt) * random.uniform(0.5, 1.5))
                continue

            outcome = Outcome(
                result.status, result.final_text, diff_snapshots(before, snapshot(ws))
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
            return {
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
                "steps": result.steps,
                "tool_calls": len(tools),
                "tool_errors": sum(e.is_error for e in tools),
                "latency_s": round(sum(e.latency_ms for e in llm) / 1000, 2),
                "wall_s": round(wall_s, 2),
                "attempts": attempt,
                "meta": {
                    "changed": outcome.changed,
                    "final_text": result.final_text[-2000:],
                    "leak_suspect": _leak_suspect(events),
                    "events": str(run_dir.relative_to(vdir)),
                    "harness_sha": harness_sha(),
                },
            }
    return None


async def run_suite(tasks: list[Task], cfg: RunConfig) -> None:
    vdir = cfg.variant_dir
    vdir.mkdir(parents=True, exist_ok=True)
    _write_state_file(cfg.results_dir)
    done = done_keys(vdir)
    todo = [(t, r) for t in tasks for r in range(cfg.reps) if (t.id, r) not in done]
    if len(done):
        print(f"resuming: {len(done)} trial(s) already scored, {len(todo)} to run")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(task: Task, rep: int) -> None:
        async with sem:
            row = await run_trial(task, rep, cfg)
        if row is None:
            print(f"  {task.id} rep{rep}: NOT SCORED (see errors.jsonl)")
            return
        _append(vdir / "results.jsonl", row)  # written as each trial completes
        mark = "PASS" if row["grade"]["passed"] else "fail"
        leak = "  [LEAK SUSPECT]" if row["meta"]["leak_suspect"] else ""
        print(f"  {task.id} rep{rep}: {mark} · {row['steps']} steps · {row['wall_s']}s{leak}")

    await asyncio.gather(*(one(t, r) for t, r in todo))


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


def cost_usd(model: str, usage: dict[str, int]) -> float | None:
    price = next((p for m, p in PRICES.items() if model.startswith(m)), None)
    if price is None:
        return None
    pin, pout = price
    return (
        usage["input_tokens"] * pin
        + usage["cache_creation_input_tokens"] * pin * 1.25
        + usage["cache_read_input_tokens"] * pin * 0.1
        + usage["output_tokens"] * pout
    ) / 1e6


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(variant_dir: Path) -> str:
    res_path, err_path = variant_dir / "results.jsonl", variant_dir / "errors.jsonl"
    rows = (
        [json.loads(x) for x in res_path.read_text().splitlines() if x] if res_path.exists() else []
    )
    errs = (
        [json.loads(x) for x in err_path.read_text().splitlines() if x] if err_path.exists() else []
    )
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
    truncated = len(rows) - n
    if truncated or errs:
        classes = Counter(e["class"] for e in errs)
        lines.append(f"- NOT SCORED: {truncated} truncated, errors {dict(classes)}")
    if leaks := [r for r in ok if r["meta"].get("leak_suspect")]:
        lines.append(f"- WARNING: {len(leaks)} trial(s) touched eval files; inspect their traces")
    lines += ["", "| task | tags | passed | steps | reason (last rep) |", "|---|---|---|---|---|"]
    for tid, rs in sorted(by_task.items()):
        rs = sorted(rs, key=lambda r: r["rep"])
        passed = "".join("✓" if r["grade"]["passed"] else "✗" for r in rs)
        steps = ",".join(str(r["steps"]) for r in rs)
        reason = rs[-1]["explanation"]["passed"].replace("|", "/")[:80]
        lines.append(f"| {tid} | {' '.join(rs[0]['tags'])} | {passed} | {steps} | {reason} |")
    return "\n".join(lines)
