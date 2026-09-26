"""Builds and runs agent loops. Used in-process by `kama run --local` and the eval
harness (run_goal), and by the daemon's RunManager (build_loop)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.agent.loop import AgentLoop, Approver, RunResult
from kama_claude.core.agent.sinks import EventSink, FanoutSink, JsonlEventWriter
from kama_claude.core.config import Settings
from kama_claude.core.llm.anthropic_provider import AnthropicProvider
from kama_claude.core.llm.types import LLMProvider
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.registry import ToolRegistry


def new_run_id() -> str:
    # Sortable by time; random suffix avoids collisions between concurrent runs.
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def runs_root(settings: Settings, workspace: Path) -> Path:
    """Absolute runs_dir; a relative setting is taken relative to the workspace."""
    return (workspace / settings.runs_dir.expanduser()).resolve()


def make_provider(settings: Settings) -> LLMProvider:
    key = settings.anthropic_api_key
    return AnthropicProvider(
        model=settings.model,
        max_tokens=settings.max_tokens,
        effort=settings.effort,
        refusal_fallback=settings.refusal_fallback,
        api_key=key.get_secret_value() if key else None,
    )


def build_loop(
    settings: Settings,
    *,
    workspace: Path,
    sink: EventSink,
    approver: Approver,
    provider: LLMProvider | None = None,
) -> AgentLoop:
    return AgentLoop(
        provider=provider or make_provider(settings),
        registry=ToolRegistry(builtin_tools()),
        sink=sink,
        workspace=workspace,
        approver=approver,
        max_steps=settings.max_steps,
    )


async def run_goal(
    goal: str,
    *,
    settings: Settings,
    workspace: Path,
    approver: Approver,
    provider: LLMProvider | None = None,
    extra_sink: EventSink | None = None,
) -> tuple[RunResult, Path]:
    """Run one goal to completion in this process. Returns the result and the run dir."""
    run_id = new_run_id()
    run_dir = runs_root(settings, workspace) / run_id
    writer = JsonlEventWriter(run_dir / "events.jsonl")
    sink: EventSink = FanoutSink(writer, extra_sink) if extra_sink else writer
    loop = build_loop(
        settings, workspace=workspace, sink=sink, approver=approver, provider=provider
    )
    try:
        result = await loop.run(goal, run_id)
    finally:
        writer.close()
    return result, run_dir
