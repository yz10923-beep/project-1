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

Planning (S3): each run gets a fresh Plan that only the task_* tools change. After a
tool changes it the loop emits plan.updated (a snapshot), and if the model ends its turn
with open tasks it is reminded once instead of the run finishing: the plan turns
"stopped early" from invisible into something the runtime can detect.
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

from kama_claude.core.agent.history import start_run_messages
from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.sinks import EventSink
from kama_claude.core.bus.events import (
    LLMDeltaEvent,
    LLMResponseEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
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
from kama_claude.core.plan import (
    PLAN_TOOL_NAMES,
    NewTask,
    Plan,
    PlanError,
    PlanTask,
    TaskChange,
    describe_changes,
    render_task,
    render_tasks,
)
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


def plan_step_allowance(max_steps: int) -> int:
    """Plan-only steps (the model only updated its plan) don't use up max_steps, up to this
    many. Measured on Haiku: planning trials spent ~7 of their steps on bookkeeping, so a
    planning agent ran out of budget for the actual work. With the allowance both A/B arms
    get the same budget for work; the cap still bounds a runaway bookkeeping loop."""
    return max_steps // 2


def plan_notice(changes: list[str], tasks: list[PlanTask]) -> str:
    listed = "\n".join(f"- {c}" for c in changes)
    return (
        f"[The user changed your plan while you were working]\n{listed}\n\n"
        f"{render_tasks(tasks)}\n\nTake this into account from now on."
    )


# One reminder is enough to catch an early stop; more would fight a model that has a
# good reason to stop (blocked, needs the user) and could loop until max_steps.
MAX_PLAN_REMINDERS = 1


def plan_reminder(open_tasks: list[PlanTask], all_tasks: list[PlanTask]) -> str:
    lines = "\n".join(render_task(t, all_tasks) for t in open_tasks)
    return (
        "You ended your turn, but your plan still has unfinished tasks:\n"
        f"{lines}\n\n"
        "Continue with them. If a task is no longer needed, cancel it with a reason; if "
        "you are blocked and need the user, say so. Then give your final answer."
    )


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
    plan_reminders: int = 0
    plan_only_steps: int = 0
    budget_credit: int = 0  # plan-only steps not counted against max_steps
    last_step_plan_only: bool = False

    @property
    def budget_used(self) -> int:
        return self.steps - self.budget_credit


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
        # The system prompt only mentions planning when the tools are really there.
        self._planning = all(registry.get(n) is not None for n in PLAN_TOOL_NAMES)
        self._state = _RunState()
        self._run_id: str | None = None  # set while a run is in progress
        self._run_span_id: str | None = None
        # task id -> (wall-clock start, monotonic start) while the task is in_progress
        self._task_clocks: dict[int, tuple[int, int]] = {}
        self._pending_notices: list[str] = []  # user plan edits not yet shown to the model

    def _meta(self, run_id: str) -> dict[str, Any]:
        meta = {"run_id": run_id, "seq": self._seq, "at": datetime.now(UTC)}
        self._seq += 1
        return meta

    async def run(
        self,
        goal: str,
        run_id: str,
        *,
        history: list[Message] | None = None,
        preamble: str | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        """Run one goal. In a session, `history` is the conversation so far (rebuilt from
        earlier runs' events) and `preamble` the memory block sent before the goal."""
        with self._tracer.span("run", "agent", model=self._provider.model) as span:
            self._run_span_id, self._task_clocks = span.span_id, {}
            try:
                result = await self._run(goal, run_id, history or [], preamble, session_id)
            finally:
                self._close_task_spans()
            span.set(status=result.status, steps=result.steps, **result.usage.model_dump())
            if self._planning:
                counts = self._ctx.plan.counts()
                span.set(
                    **{f"plan_{k}": v for k, v in counts.items()},
                    plan_reminders=self._state.plan_reminders,
                    plan_budget_credit=self._state.budget_credit,
                )
            if result.status != "completed":
                span.fail(result.error or result.status)
            return result

    def _observe_plan(self, before: list[PlanTask], after: list[PlanTask]) -> None:
        """Time each task from in_progress to whatever ends it, as a `plan` span."""
        prev = {t.id: t.status for t in before}
        for t in after:
            was, now = prev.get(t.id), t.status
            if now == "in_progress" and was != "in_progress":
                self._task_clocks.setdefault(t.id, (time.time_ns(), time.perf_counter_ns()))
            elif was == "in_progress" and now != "in_progress":
                self._record_task_span(t)

    def _record_task_span(self, t: PlanTask, still_open: bool = False) -> None:
        clock = self._task_clocks.pop(t.id, None)
        if clock is None:
            return
        start_ns, t0 = clock
        self._tracer.record(
            f"task {t.id}: {t.title}"[:80],
            "plan",
            start_ns=start_ns,
            duration_ns=time.perf_counter_ns() - t0,
            parent_id=self._run_span_id,
            status="error" if still_open else ("cancelled" if t.status == "cancelled" else "ok"),
            error="still in progress when the run ended" if still_open else None,
            attrs={"task_id": t.id, "title": t.title, "outcome": t.status, "by": t.added_by},
        )

    def _close_task_spans(self) -> None:
        for t in self._ctx.plan.tasks:
            if t.id in self._task_clocks:
                self._record_task_span(t, still_open=True)

    async def _run(
        self,
        goal: str,
        run_id: str,
        history: list[Message],
        preamble: str | None,
        session_id: str | None,
    ) -> RunResult:
        t0 = time.monotonic()
        self._seq = 0
        self._ctx = ToolContext(workspace=self._ctx.workspace, plan=Plan())
        state = self._state = _RunState()
        self._pending_notices = []
        self._run_id = run_id
        messages, repaired = start_run_messages(history, goal, preamble)
        await self._sink.emit(
            RunStartedEvent(
                **self._meta(run_id),
                goal=goal,
                model=self._provider.model,
                workspace=str(self._ctx.workspace),
                max_steps=self._max_steps,
                planning=self._planning,
                session_id=session_id,
                history_messages=len(history),
                repaired=repaired,
                preamble=preamble,
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
                    plan_only_steps=state.plan_only_steps,
                    budget_credit=state.budget_credit,
                )
            )
            return RunResult(run_id, status, text, state.steps, state.usage, error, retryable)

        system = system_prompt(self._ctx.workspace, planning=self._planning)
        tools = self._registry.specs()

        try:
            allowance = plan_step_allowance(self._max_steps) if self._planning else 0
            while state.budget_used < self._max_steps:
                state.steps += 1
                with self._tracer.span(f"step {state.steps}", "agent", step=state.steps):
                    outcome = await self._step(run_id, state, system, tools, messages)
                if outcome is not None:
                    return await finish(
                        outcome.status, outcome.text, outcome.error, outcome.retryable
                    )
                if state.last_step_plan_only and state.budget_credit < allowance:
                    state.budget_credit += 1
            credit = f" (+{state.budget_credit} plan-only)" if state.budget_credit else ""
            return await finish(
                "max_steps", error=f"no final answer after {self._max_steps} steps{credit}"
            )
        except asyncio.CancelledError:
            await finish("cancelled", error="run was cancelled")
            raise
        except Exception as e:
            # A bug, not a model or API failure. Record it so the run is never left
            # without run.finished; the traceback goes to the log.
            logger.exception("run %s crashed", run_id)
            return await finish("error", error=f"internal error: {type(e).__name__}: {e}")
        finally:
            self._run_id = None

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
        state.last_step_plan_only = False
        await self._deliver_notices(run_id, step, messages)

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
                if await self._remind_open_tasks(run_id, state, messages):
                    return None
                return _Finish("completed", resp.text)
            case "tool_use":
                calls = resp.tool_calls
                if not calls:
                    return _Finish("error", error="stop_reason=tool_use without calls")
                results = [await self._run_tool(run_id, step, c) for c in calls]
                messages.append({"role": "user", "content": results})
                if all(c.name in PLAN_TOOL_NAMES for c in calls):
                    state.plan_only_steps += 1
                    state.last_step_plan_only = True
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

    async def _remind_open_tasks(
        self, run_id: str, state: _RunState, messages: list[Message]
    ) -> bool:
        """If the model stopped with open tasks, send it the plan back (once). True if sent."""
        open_tasks = self._ctx.plan.open_tasks()
        if not open_tasks or state.plan_reminders >= MAX_PLAN_REMINDERS:
            return False
        state.plan_reminders += 1
        text = plan_reminder(open_tasks, self._ctx.plan.tasks)
        await self._sink.emit(
            PlanReminderEvent(
                **self._meta(run_id),
                step=state.steps,
                open_task_ids=[t.id for t in open_tasks],
                text=text,
            )
        )
        messages.append({"role": "user", "content": text})
        return True

    async def edit_plan(
        self, add: list[NewTask], changes: list[TaskChange]
    ) -> tuple[list[PlanTask], list[str]]:
        """A user edit to the live plan (S3 steering). Applied now; the model hears about it
        at its next call. Raises PlanError (invalid edit, planning off, run not running)."""
        if not self._planning:
            raise PlanError("planning is off for this run")
        if self._run_id is None:
            raise PlanError("the run is not running")
        before = self._ctx.plan.tasks
        self._ctx.plan.apply(add, changes, by="user")
        after = self._ctx.plan.tasks
        self._observe_plan(before, after)
        lines = describe_changes(before, after)
        # No await between the change and the event: it is recorded in plan order.
        await self._sink.emit(
            PlanUpdatedEvent(
                **self._meta(self._run_id),
                step=self._state.steps,
                tool_use_id=None,
                tasks=after,
                by="user",
                summary="; ".join(lines),
            )
        )
        self._pending_notices += lines
        return after, lines

    async def _deliver_notices(self, run_id: str, step: int, messages: list[Message]) -> None:
        """Append pending user plan edits to the user message about to be sent. It has not
        been sent yet, so history stays append-only. After a pause_turn the last message is
        the assistant's; the notice then waits for the next user message."""
        if not self._pending_notices or messages[-1]["role"] != "user":
            return
        text = plan_notice(self._pending_notices, self._ctx.plan.tasks)
        self._pending_notices = []
        content = messages[-1]["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        messages[-1] = {"role": "user", "content": [*blocks, {"type": "text", "text": text}]}
        await self._sink.emit(PlanNoticeEvent(**self._meta(run_id), step=step, text=text))

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
        plan_version = self._ctx.plan.version
        plan_before = self._ctx.plan.tasks if call.name in PLAN_TOOL_NAMES else []
        with self._tracer.span(f"tool {call.name}", "tool", tool=call.name) as span:
            out = await self._approve_and_execute(run_id, step, call)
            span.set(denied=out.denied, output_chars=len(out.result.content))
            if out.result.is_error:
                span.fail("denied" if out.denied else out.result.content[:200])
        # Only a plan tool can have changed the plan on the model's behalf: a user edit
        # that lands while (say) bash runs has already been recorded as by="user".
        if call.name in PLAN_TOOL_NAMES and self._ctx.plan.version != plan_version:
            self._observe_plan(plan_before, self._ctx.plan.tasks)
            await self._sink.emit(
                PlanUpdatedEvent(
                    **self._meta(run_id), step=step, tool_use_id=call.id, tasks=self._ctx.plan.tasks
                )
            )
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
