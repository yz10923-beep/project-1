"""The agent loop: call the model, run the tools it asks for, feed results back, repeat.

Invariants:
- History is append-only. Earlier turns are never edited, so provider-side prompt
  caching and thinking-block validity both hold.
- Every tool_use block gets exactly one tool_result, in the same order, all in a
  single user message (splitting them teaches the model to stop calling tools in parallel).
- Every run emits run.started first and run.finished last, including on API errors,
  internal bugs and cancellation.

Tracing: the loop opens one span per run, step, model call and tool (with approval and
execution as children), so a trace shows where the time and tokens went.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.sinks import EventSink
from kama_claude.core.bus.events import (
    LLMDeltaEvent,
    LLMResponseEvent,
    RunFinishedEvent,
    RunStartedEvent,
    RunStatus,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from kama_claude.core.llm.pricing import cost_usd
from kama_claude.core.llm.types import LLMError, LLMProvider, Message, ToolCall, Usage
from kama_claude.core.tools.base import ToolContext, ToolResult
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.tracer import Tracer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    by: str  # user | auto | timeout | ...


# Decides whether a side-effecting tool call may run. May return a plain bool (taken as
# the user's answer). S5 replaces this with a policy engine.
type Approver = Callable[[ToolCall], Awaitable[bool | ApprovalDecision]]

DENIED_MESSAGE = "The user denied this tool call. Do not retry it; choose another approach or stop."


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_text: str
    steps: int
    usage: Usage
    error: str | None = None
    retryable: bool | None = None  # set when the run failed on an LLM API error


@dataclass
class _RunState:
    steps: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class _Finish:
    """Returned by a step that ends the run."""

    status: RunStatus
    text: str = ""
    error: str | None = None
    retryable: bool | None = None


@dataclass(frozen=True)
class _ToolOutcome:
    result: ToolResult
    denied: bool
    approval_ms: int
    duration_ms: int


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class AgentLoop:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        sink: EventSink,
        workspace: Path,
        approver: Approver,
        max_steps: int = 30,
        tracer: Tracer | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._sink = sink
        self._ctx = ToolContext(workspace=workspace.resolve())
        self._approver = approver
        self._max_steps = max_steps
        self._tracer = tracer or Tracer.noop()
        self._seq = 0

    def _meta(self, run_id: str) -> dict[str, Any]:
        meta = {"run_id": run_id, "seq": self._seq, "at": datetime.now(UTC)}
        self._seq += 1
        return meta

    async def run(self, goal: str, run_id: str) -> RunResult:
        with self._tracer.span("run", "agent", model=self._provider.model) as span:
            result = await self._run(goal, run_id)
            span.set(status=result.status, steps=result.steps, **result.usage.model_dump())
            if result.status != "completed":
                span.fail(result.error or result.status)
            return result

    async def _run(self, goal: str, run_id: str) -> RunResult:
        t0 = time.monotonic()
        self._seq = 0
        state = _RunState()
        await self._sink.emit(
            RunStartedEvent(
                **self._meta(run_id),
                goal=goal,
                model=self._provider.model,
                workspace=str(self._ctx.workspace),
                max_steps=self._max_steps,
            )
        )

        async def finish(
            status: RunStatus,
            text: str = "",
            error: str | None = None,
            retryable: bool | None = None,
        ) -> RunResult:
            await self._sink.emit(
                RunFinishedEvent(
                    **self._meta(run_id),
                    status=status,
                    final_text=text,
                    steps=state.steps,
                    usage=state.usage,
                    duration_ms=_ms_since(t0),
                    error=error,
                    retryable=retryable,
                )
            )
            return RunResult(run_id, status, text, state.steps, state.usage, error, retryable)

        system = system_prompt(self._ctx.workspace)
        tools = self._registry.specs()
        messages: list[Message] = [{"role": "user", "content": goal}]

        try:
            while state.steps < self._max_steps:
                state.steps += 1
                with self._tracer.span(f"step {state.steps}", "agent", step=state.steps):
                    outcome = await self._step(run_id, state, system, tools, messages)
                if outcome is not None:
                    return await finish(
                        outcome.status, outcome.text, outcome.error, outcome.retryable
                    )
            return await finish("max_steps", error=f"no final answer after {self._max_steps} steps")
        except asyncio.CancelledError:
            await finish("cancelled", error="run was cancelled")
            raise
        except Exception as e:
            # A bug, not a model or API failure. Record it so the run is never left
            # without run.finished; the traceback goes to the log.
            logger.exception("run %s crashed", run_id)
            return await finish("error", error=f"internal error: {type(e).__name__}: {e}")

    async def _step(
        self,
        run_id: str,
        state: _RunState,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[Message],
    ) -> _Finish | None:
        """One model call plus the tools it asks for. Returns _Finish to end the run."""
        step = state.steps

        async def on_text(text: str) -> None:
            await self._sink.emit(LLMDeltaEvent(run_id=run_id, step=step, text=text))

        t_llm = time.monotonic()
        with self._tracer.span("llm.call", "llm", step=step) as llm_span:
            try:
                resp = await self._provider.complete(
                    system=system, messages=messages, tools=tools, on_text=on_text
                )
            except LLMError as e:
                llm_span.fail(str(e))
                return _Finish("error", error=str(e), retryable=e.retryable)
            usage = resp.usage.model_dump()
            llm_span.set(
                model=resp.model,
                stop_reason=resp.stop_reason,
                ttft_ms=resp.ttft_ms,
                cost_usd=cost_usd(resp.model, usage),
                **usage,
            )
        state.usage = state.usage + resp.usage
        await self._sink.emit(
            LLMResponseEvent(
                **self._meta(run_id),
                step=step,
                stop_reason=resp.stop_reason,
                content=resp.content,
                usage=resp.usage,
                latency_ms=_ms_since(t_llm),
                model=resp.model,
                ttft_ms=resp.ttft_ms,
            )
        )
        messages.append({"role": "assistant", "content": resp.content})

        match resp.stop_reason:
            case "end_turn" | "stop_sequence":
                return _Finish("completed", resp.text)
            case "tool_use":
                calls = resp.tool_calls
                if not calls:
                    return _Finish("error", error="stop_reason=tool_use without calls")
                results = [await self._run_tool(run_id, step, c) for c in calls]
                messages.append({"role": "user", "content": results})
                return None
            case "pause_turn":
                # Server-side tool paused mid-turn; resending the history resumes it.
                return None
            case "max_tokens":
                return _Finish("truncated", resp.text, "response hit max_tokens")
            case "refusal":
                return _Finish("refused", resp.text, "model declined the request")
            case _:
                return _Finish("error", resp.text, f"unexpected stop_reason: {resp.stop_reason}")

    async def _run_tool(self, run_id: str, step: int, call: ToolCall) -> dict[str, Any]:
        """Execute one tool call and return its tool_result block. Never raises (except cancel)."""
        await self._sink.emit(
            ToolStartedEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                name=call.name,
                input=call.input,
            )
        )
        with self._tracer.span(f"tool {call.name}", "tool", tool=call.name) as span:
            out = await self._approve_and_execute(run_id, step, call)
            span.set(denied=out.denied, output_chars=len(out.result.content))
            if out.result.is_error:
                span.fail("denied" if out.denied else out.result.content[:200])
        await self._sink.emit(
            ToolFinishedEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                name=call.name,
                is_error=out.result.is_error,
                denied=out.denied,
                output=out.result.content,
                duration_ms=out.duration_ms,
                approval_ms=out.approval_ms,
            )
        )
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": out.result.content,
        }
        if out.result.is_error:
            block["is_error"] = True
        return block

    async def _approve_and_execute(self, run_id: str, step: int, call: ToolCall) -> _ToolOutcome:
        # Human wait and tool execution are timed separately: mixing them makes tool
        # latency and trajectory-efficiency numbers meaningless.
        approval_ms = 0
        tool = self._registry.get(call.name)
        if tool is not None and tool.requires_approval:
            await self._sink.emit(
                ToolApprovalRequestedEvent(
                    **self._meta(run_id),
                    step=step,
                    tool_use_id=call.id,
                    name=call.name,
                    input=call.input,
                )
            )
            t_wait = time.monotonic()
            with self._tracer.span("tool.approval", "tool") as approval_span:
                answer = await self._approver(call)
                decision = (
                    answer
                    if isinstance(answer, ApprovalDecision)
                    else ApprovalDecision(answer, "user")
                )
                approval_span.set(approved=decision.approved, by=decision.by)
            approval_ms = _ms_since(t_wait)
            await self._sink.emit(
                ToolApprovalResolvedEvent(
                    **self._meta(run_id),
                    step=step,
                    tool_use_id=call.id,
                    approved=decision.approved,
                    by=decision.by,
                )
            )
            if not decision.approved:
                return _ToolOutcome(ToolResult(DENIED_MESSAGE, is_error=True), True, approval_ms, 0)
        t_exec = time.monotonic()
        with self._tracer.span("tool.exec", "tool"):
            result = await self._registry.execute(call.name, call.input, self._ctx)
        return _ToolOutcome(result, False, approval_ms, _ms_since(t_exec))
