"""Builds and runs agent loops. Used in-process by `kama run --local` and the eval
harness (run_goal), and by the daemon's RunManager (build_loop)."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from kama_claude.core.agent.loop import AgentLoop, Approver, RememberRules, RunResult
from kama_claude.core.agent.sinks import EventSink, FanoutSink, JsonlEventWriter
from kama_claude.core.config import Settings
from kama_claude.core.llm.anthropic_provider import AnthropicProvider
from kama_claude.core.llm.retry import RetryPolicy
from kama_claude.core.llm.types import LLMProvider, Message
from kama_claude.core.notes import NoteStore, memory_preamble
from kama_claude.core.policy.engine import Mode, Policy, Rule
from kama_claude.core.sandbox import Sandbox, detect
from kama_claude.core.session import SessionStore
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.note_tools import note_tools
from kama_claude.core.tools.plan_tools import plan_tools
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.tracer import JsonlSpanWriter, Tracer

TRACE_FILE = "trace.jsonl"


def new_run_id() -> str:
    # Sortable by time; random suffix avoids collisions between concurrent runs.
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def runs_root(settings: Settings, workspace: Path) -> Path:
    """Absolute runs_dir; a relative setting is taken relative to the workspace."""
    return (workspace / settings.runs_dir.expanduser()).resolve()


def make_provider(
    settings: Settings, client: anthropic.AsyncAnthropic | None = None
) -> LLMProvider:
    """Pass `client` to reuse one SDK client (and its connection pool) across runs."""
    key = settings.anthropic_api_key
    return AnthropicProvider(
        model=settings.model,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        refusal_fallback=settings.refusal_fallback,
        api_key=key.get_secret_value() if key else None,
        client=client,
    )


def run_tracer(run_id: str, run_dir: Path) -> Tracer:
    """Spans for one run go to <run_dir>/trace.jsonl, next to events.jsonl."""
    return Tracer(run_id, JsonlSpanWriter(run_dir / TRACE_FILE))


@dataclass(frozen=True)
class RunContext:
    """What a run starts from besides its goal (S4): the session's conversation so far and
    the memory block (notes, and a staleness reminder when the session continues)."""

    history: list[Message]
    preamble: str | None


def prepare_run(
    settings: Settings,
    workspace: Path,
    session_id: str | None,
    sessions: SessionStore,
    notes: NoteStore | None,
) -> RunContext:
    if not settings.memory:
        return RunContext([], None)
    history: list[Message] = []
    continued_from = None
    if session_id is not None:
        history = sessions.history(session_id)
        finished = [r.finished_at for r in sessions.get(session_id).runs if r.finished_at]
        continued_from = max(finished) if history and finished else None
    visible = notes.visible(workspace, session_id) if notes is not None else []
    return RunContext(history, memory_preamble(visible, continued_from=continued_from))


def note_store(settings: Settings) -> NoteStore | None:
    return NoteStore(settings.memory_dir) if settings.memory else None


def private_paths(settings: Settings) -> tuple[Path, ...]:
    """Extra paths from KAMA_PRIVATE_PATHS (e.g. the eval graders)."""
    return tuple(
        Path(p.strip()).expanduser() for p in settings.private_paths.split(",") if p.strip()
    )


def daemon_paths(settings: Settings) -> tuple[Path, ...]:
    """The daemon's own files and dirs, plus KAMA_PRIVATE_PATHS: no tool call may read or
    change them."""
    own = (
        settings.runs_dir,
        settings.sessions_dir,
        settings.memory_dir,
        settings.token_file,
        settings.policy_file,
    )
    return tuple(p.expanduser() for p in own if p.expanduser().is_absolute()) + private_paths(
        settings
    )


def load_policy(
    settings: Settings,
    workspace: Path,
    *,
    mode: Mode | None,
    session_rules: list[Rule] | None = None,
) -> Policy | None:
    """The run's policy, or None with KAMA_POLICY=false. Raises PolicyFileError."""
    if not settings.policy:
        return None
    return Policy.load(
        workspace,
        mode=mode,
        user_file=settings.policy_file,
        kama_paths=daemon_paths(settings),
        session_rules=session_rules,
    )


def sandbox_for(settings: Settings) -> Sandbox:
    """Probed once per process (cached). Raises SandboxUnavailable for an explicit backend
    that doesn't work here."""
    return detect(settings.sandbox)


def retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(max_retries=settings.llm_max_retries, budget_s=settings.llm_retry_budget_s)


def build_loop(
    settings: Settings,
    *,
    workspace: Path,
    sink: EventSink,
    approver: Approver,
    provider: LLMProvider | None = None,
    tracer: Tracer | None = None,
    notes: NoteStore | None = None,
    mode: Mode | None = None,
    session_rules: list[Rule] | None = None,
    on_remember: RememberRules | None = None,
) -> AgentLoop:
    tools = builtin_tools()
    tools += plan_tools() if settings.planning else []
    tools += note_tools() if settings.memory else []
    keep = frozenset(k.strip() for k in settings.bash_env_keep.split(",") if k.strip())
    return AgentLoop(
        provider=provider or make_provider(settings),
        registry=ToolRegistry(tools),
        sink=sink,
        workspace=workspace,
        approver=approver,
        max_steps=settings.max_steps,
        tracer=tracer,
        notes=notes if settings.memory else None,
        policy=load_policy(settings, workspace, mode=mode, session_rules=session_rules),
        sandbox=sandbox_for(settings),
        retry=retry_policy(settings),
        on_remember=on_remember,
        env_keep=keep,
        hidden=private_paths(settings),
    )


async def run_goal(
    goal: str,
    *,
    settings: Settings,
    workspace: Path,
    approver: Approver,
    provider: LLMProvider | None = None,
    extra_sink: EventSink | None = None,
    session_id: str | None = None,
    sessions: SessionStore | None = None,
    mode: Mode | None = None,
) -> tuple[RunResult, Path]:
    """Run one goal to completion in this process. Returns the result and the run dir.
    With `session_id`, the run continues that session (its history, unless memory is
    off) and is recorded in it."""
    run_id = new_run_id()
    run_dir = runs_root(settings, workspace) / run_id
    store = sessions or SessionStore(settings.sessions_dir)
    notes = note_store(settings)
    context = prepare_run(settings, workspace, session_id, store, notes)
    if session_id is not None:
        store.add_run(session_id, run_id, run_dir, goal)
    writer = JsonlEventWriter(run_dir / "events.jsonl")
    sink: EventSink = FanoutSink(writer, extra_sink) if extra_sink else writer
    session_rules = store.rules(session_id) if session_id is not None else None

    async def remember(rules: tuple[Rule, ...]) -> None:
        if session_id is not None:
            await asyncio.to_thread(store.add_rules, session_id, list(rules))

    loop = build_loop(
        settings,
        workspace=workspace,
        sink=sink,
        approver=approver,
        provider=provider,
        tracer=run_tracer(run_id, run_dir),
        notes=notes,
        mode=mode,
        session_rules=session_rules,
        on_remember=remember,
    )
    status = "error"
    try:
        result = await loop.run(
            goal,
            run_id,
            history=context.history,
            preamble=context.preamble,
            session_id=session_id,
        )
        status = result.status
    except BaseException:
        status = "cancelled"
        raise
    finally:
        writer.close()
        if session_id is not None:
            store.finish_run(session_id, run_id, status)
    return result, run_dir
