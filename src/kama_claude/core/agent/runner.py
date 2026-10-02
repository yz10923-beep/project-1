"""Builds and runs agent loops. Used in-process by `kama run --local` and the eval
harness (run_goal), and by the daemon's RunManager (build_loop)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from kama_claude.core.agent.loop import AgentLoop, Approver, RunResult
from kama_claude.core.agent.sinks import EventSink, FanoutSink, JsonlEventWriter
from kama_claude.core.config import Settings
from kama_claude.core.llm.anthropic_provider import AnthropicProvider
from kama_claude.core.llm.types import LLMProvider
from kama_claude.core.session import SessionStore
from kama_claude.core.tools.builtin import builtin_tools
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


def build_loop(
    settings: Settings,
    *,
    workspace: Path,
    sink: EventSink,
    approver: Approver,
    provider: LLMProvider | None = None,
    tracer: Tracer | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider or make_provider(settings),
        registry=ToolRegistry(builtin_tools() + (plan_tools() if settings.planning else [])),
        sink=sink,
        workspace=workspace,
        approver=approver,
        max_steps=settings.max_steps,
        tracer=tracer,
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
) -> tuple[RunResult, Path]:
    """Run one goal to completion in this process. Returns the result and the run dir.
    With `session_id`, the run continues that session (its history, unless memory is
    off) and is recorded in it."""
    run_id = new_run_id()
    run_dir = runs_root(settings, workspace) / run_id
    store = sessions or SessionStore(settings.sessions_dir)
    history = store.history(session_id) if session_id is not None and settings.memory else []
    if session_id is not None:
        store.add_run(session_id, run_id, run_dir, goal)
    writer = JsonlEventWriter(run_dir / "events.jsonl")
    sink: EventSink = FanoutSink(writer, extra_sink) if extra_sink else writer
    loop = build_loop(
        settings,
        workspace=workspace,
        sink=sink,
        approver=approver,
        provider=provider,
        tracer=run_tracer(run_id, run_dir),
    )
    status = "error"
    try:
        result = await loop.run(goal, run_id, history=history, session_id=session_id)
        status = result.status
    except BaseException:
        status = "cancelled"
        raise
    finally:
        writer.close()
        if session_id is not None:
            store.finish_run(session_id, run_id, status)
    return result, run_dir
