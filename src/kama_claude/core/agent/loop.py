"""The agent loop: call the model, run the tools it asks for, feed results back, repeat.

Invariants:
- History is append-only. Earlier turns are never edited, so provider-side prompt
  caching and thinking-block validity both hold.
- Every tool_use block gets exactly one tool_result, in the same order, all in a
  single user message (splitting them teaches the model to stop calling tools in parallel).
- Every run emits run.started first and run.finished last, including on API errors,
  internal bugs and cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.sinks import EventSink
from kama_claude.core.bus.events import (
    LLMResponseEvent,
    RunFinishedEvent,
    RunStartedEvent,
    RunStatus,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from kama_claude.core.llm.types import LLMError, LLMProvider, Message, ToolCall, Usage
from kama_claude.core.tools.base import ToolContext, ToolResult
from kama_claude.core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Decides whether a side-effecting tool call may run. S5 replaces this with a policy engine.
type Approver = Callable[[ToolCall], Awaitable[bool]]

DENIED_MESSAGE = "The user denied this tool call. Do not retry it; choose another approach or stop."


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    final_text: str
    steps: int
    usage: Usage
    error: str | None = None


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
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._sink = sink
        self._ctx = ToolContext(workspace=workspace.resolve())
        self._approver = approver
        self._max_steps = max_steps
        self._seq = 0

    def _meta(self, run_id: str) -> dict[str, Any]:
        meta = {"run_id": run_id, "seq": self._seq, "at": datetime.now(UTC)}
        self._seq += 1
        return meta

    async def run(self, goal: str, run_id: str) -> RunResult:
        t0 = time.monotonic()
        self._seq = 0
        usage = Usage()
        steps = 0
        await self._sink.emit(
            RunStartedEvent(
                **self._meta(run_id),
                goal=goal,
                model=self._provider.model,
                workspace=str(self._ctx.workspace),
                max_steps=self._max_steps,
            )
        )

        async def finish(status: RunStatus, text: str = "", error: str | None = None) -> RunResult:
            await self._sink.emit(
                RunFinishedEvent(
                    **self._meta(run_id),
                    status=status,
                    final_text=text,
                    steps=steps,
                    usage=usage,
                    duration_ms=_ms_since(t0),
                    error=error,
                )
            )
            return RunResult(run_id, status, text, steps, usage, error)

        system = system_prompt(self._ctx.workspace)
        tools = self._registry.specs()
        messages: list[Message] = [{"role": "user", "content": goal}]

        try:
            while steps < self._max_steps:
                steps += 1
                t_llm = time.monotonic()
                try:
                    resp = await self._provider.complete(
                        system=system, messages=messages, tools=tools
                    )
                except LLMError as e:
                    return await finish("error", error=str(e))
                usage = usage + resp.usage
                await self._sink.emit(
                    LLMResponseEvent(
                        **self._meta(run_id),
                        step=steps,
                        stop_reason=resp.stop_reason,
                        content=resp.content,
                        usage=resp.usage,
                        latency_ms=_ms_since(t_llm),
                    )
                )
                messages.append({"role": "assistant", "content": resp.content})

                match resp.stop_reason:
                    case "end_turn" | "stop_sequence":
                        return await finish("completed", resp.text)
                    case "tool_use":
                        calls = resp.tool_calls
                        if not calls:
                            return await finish("error", error="stop_reason=tool_use without calls")
                        results = [await self._run_tool(run_id, steps, c) for c in calls]
                        messages.append({"role": "user", "content": results})
                    case "pause_turn":
                        # Server-side tool paused mid-turn; resending the history resumes it.
                        continue
                    case "max_tokens":
                        return await finish("truncated", resp.text, "response hit max_tokens")
                    case "refusal":
                        return await finish("refused", resp.text, "model declined the request")
                    case _:
                        return await finish(
                            "error", resp.text, f"unexpected stop_reason: {resp.stop_reason}"
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
        # Human wait and tool execution are timed separately: mixing them makes tool
        # latency and trajectory-efficiency numbers meaningless.
        approval_ms = 0
        duration_ms = 0
        denied = False
        tool = self._registry.get(call.name)
        if tool is not None and tool.requires_approval:
            t_wait = time.monotonic()
            denied = not await self._approver(call)
            approval_ms = _ms_since(t_wait)
        if denied:
            result = ToolResult(DENIED_MESSAGE, is_error=True)
        else:
            t_exec = time.monotonic()
            result = await self._registry.execute(call.name, call.input, self._ctx)
            duration_ms = _ms_since(t_exec)
        await self._sink.emit(
            ToolFinishedEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                name=call.name,
                is_error=result.is_error,
                denied=denied,
                output=result.content,
                duration_ms=duration_ms,
                approval_ms=approval_ms,
            )
        )
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": result.content,
        }
        if result.is_error:
            block["is_error"] = True
        return block
