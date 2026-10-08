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
import dataclasses
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.agent.history import compacted_view, resume_text, start_run_messages
from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.sinks import EventSink
from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    LLMDeltaEvent,
    LLMResponseEvent,
    LLMRetryEvent,
    NoteUpdatedEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    RunStatus,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolPolicyEvent,
    ToolStartedEvent,
)
from kama_claude.core.context import (
    COMPACTION_INSTRUCTIONS,
    Compactor,
    ContextMeter,
    TokenCounter,
)
from kama_claude.core.llm.pricing import cost_usd
from kama_claude.core.llm.retry import NO_RETRY, RetryPolicy
from kama_claude.core.llm.types import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    Usage,
)
from kama_claude.core.notes import NoteBook, NoteStore
from kama_claude.core.outputs import LINE_MAX_CHARS, OutputStore
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
from kama_claude.core.policy.engine import Decision, Policy, Rule, call_key, denial_message
from kama_claude.core.sandbox import Sandbox
from kama_claude.core.tools.base import ToolContext, ToolResult
from kama_claude.core.tools.note_tools import NOTE_TOOL_NAMES
from kama_claude.core.tools.registry import MAX_RESULT_CHARS, ToolRegistry
from kama_claude.core.trace.tracer import Tracer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    by: str  # user | auto | timeout | ...
    reason: str = ""  # the user's reason for a denial; passed on to the model
    remember: bool = False  # "always allow": add the policy's suggested rules for the session


# Answers the calls the policy says to ask about (with policy off: every bash/write_file
# call). May return a plain bool, taken as the user's answer.
type Approver = Callable[[ToolCall], Awaitable[bool | ApprovalDecision]]
# Called when the user answers "always allow", to keep the new rules (e.g. in the session).
type RememberRules = Callable[[tuple[Rule, ...]], Awaitable[None]]

DENIED_MESSAGE = "The user denied this tool call. Do not retry it; choose another approach or stop."
# The same failing call again and again is a loop, not progress.
REPEAT_FAILURE_LIMIT = 3
COMPACT_RETRY_STEPS = 3  # after a failed compaction, steps before trying again (S6)


def denied_by_user(reason: str) -> str:
    if not reason:
        return DENIED_MESSAGE
    return (
        f"The user denied this tool call and said: {reason!r}. Do not retry it; take that "
        "into account and choose another approach, or stop."
    )


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
    notes_changed: int = 0
    # S5
    policy_denials: int = 0
    repeat_denials: int = 0
    approvals_asked: int = 0
    llm_retries: int = 0
    tool_errors: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)  # call key -> times it failed
    denied_calls: set[str] = field(default_factory=set)
    denied_rules: set[str] = field(default_factory=set)
    # S6: request sizes (measured with KAMA_CONTEXT on or off; only S6 acts on them)
    meter: ContextMeter = field(default_factory=lambda: ContextMeter(120_000))
    compactions: int = 0
    compact_after: int = 0  # after a failed compaction, wait until this step to retry
    # The first request after a compaction. If even that is over the budget (a huge goal,
    # a long summary), compacting again can't help until the context grows past it:
    # without this floor every step would pay for another summary.
    compact_floor: int = 0
    measure_floor: bool = False

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
        notes: NoteStore | None = None,
        policy: Policy | None = None,
        sandbox: Sandbox | None = None,
        retry: RetryPolicy = NO_RETRY,
        on_remember: RememberRules | None = None,
        env_keep: frozenset[str] = frozenset(),
        hidden: tuple[Path, ...] = (),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        outputs: OutputStore | None = None,
        max_result_chars: int = MAX_RESULT_CHARS,
        context_budget: int = 120_000,
        compaction: bool = False,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._sink = sink
        self._base_ctx = ToolContext(
            workspace=workspace.resolve(),
            sandbox=sandbox,
            env_keep=env_keep,
            hidden=hidden,
            # S6: None = the S1 cap (head and tail, middle lost), as in S5
            outputs=outputs,
            max_result_chars=max_result_chars,
            max_line_chars=LINE_MAX_CHARS if outputs is not None else None,
        )
        self._context_budget = context_budget
        # S6: compact when the next request would exceed the budget (needs a provider
        # that can, and a model the API compacts for; off = the history just grows)
        self._compaction = compaction and isinstance(provider, Compactor)
        self._goal = ""
        self._ctx = self._base_ctx
        # S5: None = the S4 approvals (ask for every requires_approval tool).
        self._policy = policy
        self._sandbox = sandbox
        self._retry = retry
        self._on_remember = on_remember
        self._sleep = sleep
        self._approver = approver
        self._max_steps = max_steps
        self._tracer = tracer or Tracer.noop()
        self._seq = 0
        # The system prompt only mentions planning when the tools are really there.
        self._planning = all(registry.get(n) is not None for n in PLAN_TOOL_NAMES)
        self._notes = notes
        self._memory = notes is not None and all(
            registry.get(n) is not None for n in NOTE_TOOL_NAMES
        )
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
            span.set(
                session_id=session_id,
                history_messages=len(history or []),
                memory_notes=(preamble or "").count("\n- ["),
                memory_chars=len(preamble or ""),
                notes_changed=self._state.notes_changed,
                policy_mode=self._policy.mode if self._policy else None,
                sandbox=(self._sandbox or Sandbox("none")).backend if self._policy else None,
                policy_denials=self._state.policy_denials,
                repeat_denials=self._state.repeat_denials,
                approvals_asked=self._state.approvals_asked,
                llm_retries=self._state.llm_retries,
                context_peak=self._state.meter.peak,
                compactions=self._state.compactions,
            )
            if (context := self._context_summary()) is not None:
                span.set(**{f"context_{k}": v for k, v in context.items()})
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
        self._goal = goal
        book = (
            NoteBook(self._notes, self._ctx.workspace, session_id, run_id)
            if self._memory and self._notes is not None
            else None
        )
        self._ctx = dataclasses.replace(self._base_ctx, plan=Plan(), notes=book)
        counter = self._provider if isinstance(self._provider, TokenCounter) else None
        state = self._state = _RunState(meter=ContextMeter(self._context_budget, counter))
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
                policy=self._policy_summary(),
                context=self._context_summary(),
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
                    policy_denials=state.policy_denials,
                    repeat_denials=state.repeat_denials,
                    approvals_asked=state.approvals_asked,
                    llm_retries=state.llm_retries,
                    tool_errors=dict(state.tool_errors),
                    context_peak=state.meter.peak,
                    compactions=state.compactions,
                )
            )
            return RunResult(run_id, status, text, state.steps, state.usage, error, retryable)

        system = system_prompt(self._ctx.workspace, planning=self._planning, memory=self._memory)
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
        if self._compaction:
            await self._maybe_compact(run_id, step, state, system, tools, messages)

        async def on_text(text: str) -> None:
            await self._sink.emit(LLMDeltaEvent(run_id=run_id, step=step, text=text))

        estimate = state.meter.estimate(system, tools, messages)
        attempt, waited = 0, 0.0
        while True:
            attempt += 1
            t_llm = time.monotonic()
            failed: LLMError | None = None
            with self._tracer.span("llm.call", "llm", step=step, attempt=attempt) as llm_span:
                try:
                    resp = await self._provider.complete(
                        system=system, messages=messages, tools=tools, on_text=on_text
                    )
                except LLMError as e:
                    llm_span.fail(str(e))
                    llm_span.set(error_kind=e.kind, status=e.status)
                    failed = e
                else:
                    usage = resp.usage.model_dump()
                    size = state.meter.observe(resp.usage, len(messages))
                    if state.measure_floor:
                        state.compact_floor, state.measure_floor = size, False
                    llm_span.set(context_tokens=size, context_estimate=estimate)
                    llm_span.set(
                        model=resp.model,
                        stop_reason=resp.stop_reason,
                        ttft_ms=resp.ttft_ms,
                        cost_usd=cost_usd(resp.model, usage),
                        **usage,
                    )
            if failed is None:
                break
            if failed.kind in ("context_overflow", "too_large"):
                # Not an infra failure: the agent's own history no longer fits.
                return _Finish("context_overflow", error=str(failed), retryable=False)
            delay = self._retry.delay(failed, attempt)
            if not self._retry.should_retry(failed, attempt, waited, delay):
                tries = f" (after {attempt - 1} retries)" if attempt > 1 else ""
                return _Finish("error", error=f"{failed}{tries}", retryable=failed.retryable)
            state.llm_retries += 1
            await self._sink.emit(
                LLMRetryEvent(
                    **self._meta(run_id),
                    step=step,
                    attempt=attempt,
                    kind=failed.kind,
                    error=str(failed)[:500],
                    wait_s=round(delay, 3),
                    status=failed.status,
                )
            )
            with self._tracer.span("llm.backoff", "llm", step=step, wait_s=round(delay, 3)):
                await self._sleep(delay)
            waited += delay
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
            case "model_context_window_exceeded":
                return _Finish("context_overflow", resp.text, "the model's context window is full")
            case _:
                return _Finish("error", resp.text, f"unexpected stop_reason: {resp.stop_reason}")

    async def _maybe_compact(
        self,
        run_id: str,
        step: int,
        state: _RunState,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[Message],
    ) -> None:
        """Summarize the history server-side when the next request would exceed the budget.

        Only at a step boundary: here the last message is the user turn (goal, tool
        results), never an assistant turn waiting on tool results. The whole history is
        summarized and nothing older is replayed, so no thinking block outlives the
        prefix it was made in. Messages are replaced in place; the event lets replay
        do the same."""
        # Nothing to summarize but the goal: send it as is. A continuing session has its
        # history before the goal, so its first step may compact.
        if len(messages) <= 1 or step < state.compact_after or messages[-1]["role"] != "user":
            return
        tokens, how = await state.meter.measure(system, tools, messages)
        # Hysteresis: after a compaction, the next one waits until the context has grown
        # a quarter of the budget past the summary. A summary that leaves less room than
        # that means the budget is too small for the task's working set: the run goes
        # over the budget rather than paying for a summary every step. (s6-cal dropped
        # this rule for summaries under the budget, and compacted nearly every step.)
        if tokens <= max(self._context_budget, state.compact_floor + self._context_budget // 4):
            return
        assert isinstance(self._provider, Compactor)
        t0 = time.monotonic()
        resp: LLMResponse | None = None
        failure = ""
        with self._tracer.span(
            "context.compact", "llm", step=step, tokens_before=tokens, measured=how
        ) as span:
            attempt, waited = 0, 0.0
            while True:
                attempt += 1
                try:
                    resp = await self._provider.compact(
                        system=system,
                        messages=messages,
                        tools=tools,
                        instructions=COMPACTION_INSTRUCTIONS,
                    )
                    break
                except LLMError as e:
                    delay = self._retry.delay(e, attempt)
                    if not self._retry.should_retry(e, attempt, waited, delay):
                        failure = f"{e.kind}: {e}"
                        break
                    await self._sleep(delay)
                    waited += delay
            block = (
                resp.content[0]
                if resp is not None
                and resp.stop_reason == "compaction"
                and resp.content
                and resp.content[0].get("type") == "compaction"
                else None
            )
            usage = resp.usage if resp is not None else Usage()
            state.usage = state.usage + usage
            span.set(
                cost_usd=cost_usd(resp.model if resp else "", usage.model_dump()),
                **usage.model_dump(),
            )
            if block is None:
                failure = failure or f"no summary (stop_reason {resp.stop_reason if resp else '?'})"
                span.fail(failure)
        if block is None:
            state.compact_after = step + COMPACT_RETRY_STEPS
            await self._sink.emit(
                ContextCompactionFailedEvent(
                    **self._meta(run_id),
                    step=step,
                    tokens_before=tokens,
                    reason=failure,
                    usage=usage,
                )
            )
            return
        assert resp is not None
        plan = self._ctx.plan.render() if self._ctx.plan.tasks else None
        resume = resume_text(self._goal, plan, read_output=self._ctx.outputs is not None)
        replaced = len(messages)
        messages[:] = compacted_view(block, resume)
        state.compactions += 1
        state.meter.restart()
        state.measure_floor = True
        await self._sink.emit(
            ContextCompactedEvent(
                **self._meta(run_id),
                step=step,
                block=block,
                resume=resume,
                tokens_before=tokens,
                measured=how,  # type: ignore[arg-type]  # "estimate" | "count"
                messages_replaced=replaced,
                usage=usage,
                latency_ms=_ms_since(t0),
                model=resp.model,
            )
        )

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
            if out.result.cut is not None:
                c = out.result.cut
                span.set(cut_chars=c.original_chars - c.kept_chars, output_id=c.output_id)
            if out.result.is_error:
                span.set(error_kind=out.result.error_kind)
                span.fail("denied" if out.denied else out.result.content[:200])
        out = self._guard_repeats(call, out)
        # Only a plan tool can have changed the plan on the model's behalf: a user edit
        # that lands while (say) bash runs has already been recorded as by="user".
        if call.name in PLAN_TOOL_NAMES and self._ctx.plan.version != plan_version:
            self._observe_plan(plan_before, self._ctx.plan.tasks)
            await self._sink.emit(
                PlanUpdatedEvent(
                    **self._meta(run_id), step=step, tool_use_id=call.id, tasks=self._ctx.plan.tasks
                )
            )
        if call.name in NOTE_TOOL_NAMES and self._ctx.notes is not None:
            for change in self._ctx.notes.drain():
                self._state.notes_changed += 1
                await self._sink.emit(
                    NoteUpdatedEvent(
                        **self._meta(run_id),
                        step=step,
                        tool_use_id=call.id,
                        action=change.action,
                        note=change.note,
                        reason=change.reason,
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
                error_kind=out.result.error_kind,
                cut=out.result.cut,
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

    def _guard_repeats(self, call: ToolCall, out: _ToolOutcome) -> _ToolOutcome:
        """Count failures by kind; when the exact same call has failed several times, say so
        in its result (appended to this result, so history stays append-only)."""
        if not out.result.is_error:
            return out
        state = self._state
        state.tool_errors[out.result.error_kind or "failed"] += 1
        key = call_key(call.name, call.input)
        state.failures[key] += 1
        n = state.failures[key]
        if n < REPEAT_FAILURE_LIMIT or out.result.error_kind == "blocked":
            return out  # policy denials carry their own "already tried" wording
        note = (
            f"\n\n[This exact call has now failed {n} times. Calling it again unchanged will "
            "fail the same way: change the input, use another approach, or stop and explain.]"
        )
        result = dataclasses.replace(out.result, content=out.result.content + note)
        return dataclasses.replace(out, result=result)

    def _context_summary(self) -> dict[str, Any] | None:
        if self._base_ctx.outputs is None:
            return None
        return {
            "budget": self._context_budget,
            "cap": self._base_ctx.max_result_chars,
            "compaction": self._compaction,
        }

    def _policy_summary(self) -> dict[str, Any] | None:
        if self._policy is None:
            return None
        p = self._policy
        return {
            "mode": p.mode,
            "sandbox": (self._sandbox or Sandbox("none")).backend,
            "user_rules": len(p.user_rules),
            "workspace_rules": len(p.workspace_rules),
            "session_rules": len(p.session_rules),
            "warnings": p.warnings,
        }

    async def _execute(self, call: ToolCall, network: bool) -> tuple[ToolResult, int]:
        t_exec = time.monotonic()
        ctx = (
            self._ctx
            if network == self._ctx.network
            else dataclasses.replace(self._ctx, network=network)
        )
        with self._tracer.span("tool.exec", "tool", network=network):
            result = await self._registry.execute(call.name, call.input, ctx, output_id=call.id)
        return result, _ms_since(t_exec)

    async def _ask(
        self, run_id: str, step: int, call: ToolCall, decision: Decision | None
    ) -> tuple[ApprovalDecision, int]:
        """Ask the approver (a human, through whichever client answers first)."""
        self._state.approvals_asked += 1
        await self._sink.emit(
            ToolApprovalRequestedEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                name=call.name,
                input=call.input,
                risk=decision.risk if decision else "",
                reason=decision.reason if decision else "",
                rule=decision.rule if decision else "",
                remember=[_describe_rule(r) for r in decision.remember] if decision else [],
            )
        )
        t_wait = time.monotonic()
        with self._tracer.span("tool.approval", "tool") as approval_span:
            answer = await self._approver(call)
            approval = (
                answer if isinstance(answer, ApprovalDecision) else ApprovalDecision(answer, "user")
            )
            approval_span.set(approved=approval.approved, by=approval.by)
        approval_ms = _ms_since(t_wait)
        remembered: tuple[Rule, ...] = ()
        if approval.approved and approval.remember and decision and self._policy is not None:
            remembered = decision.remember
            self._policy.remember(remembered)
            if self._on_remember is not None and remembered:
                await self._on_remember(remembered)
        await self._sink.emit(
            ToolApprovalResolvedEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                approved=approval.approved,
                by=approval.by,
                reason=approval.reason,
                remembered=[_describe_rule(r) for r in remembered],
            )
        )
        return approval, approval_ms

    async def _approve_and_execute(self, run_id: str, step: int, call: ToolCall) -> _ToolOutcome:
        # Human wait and tool execution are timed separately: mixing them makes tool
        # latency and trajectory-efficiency numbers meaningless.
        if self._policy is None:  # S4: ask for every side-effecting tool
            approval_ms = 0
            tool = self._registry.get(call.name)
            if tool is not None and tool.requires_approval:
                approval, approval_ms = await self._ask(run_id, step, call, None)
                if not approval.approved:
                    return _ToolOutcome(
                        ToolResult(denied_by_user(approval.reason), True, "denied"),
                        True,
                        approval_ms,
                        0,
                    )
            result, duration = await self._execute(call, network=True)
            return _ToolOutcome(result, False, approval_ms, duration)

        with self._tracer.span("tool.policy", "tool") as policy_span:
            decision = self._policy.check(call.name, call.input)
            policy_span.set(action=decision.action, rule=decision.rule, kind=decision.kind)
        if decision.action == "deny":
            return await self._deny(run_id, step, call, decision)
        approval_ms = 0
        if decision.action == "ask":
            approval, approval_ms = await self._ask(run_id, step, call, decision)
            if not approval.approved:
                return _ToolOutcome(
                    ToolResult(denied_by_user(approval.reason), True, "denied"),
                    True,
                    approval_ms,
                    0,
                )
        elif decision.kind != "read":
            await self._sink.emit(
                ToolPolicyEvent(
                    **self._meta(run_id),
                    step=step,
                    tool_use_id=call.id,
                    name=call.name,
                    action="allow",
                    rule=decision.rule,
                    reason=decision.reason,
                    risk=decision.risk,
                    kind=decision.kind,
                    network=decision.network,
                    effects=decision.to_event()["effects"],
                )
            )
        result, duration = await self._execute(call, network=decision.network)
        return _ToolOutcome(result, False, approval_ms, duration)

    async def _deny(
        self, run_id: str, step: int, call: ToolCall, decision: Decision
    ) -> _ToolOutcome:
        state = self._state
        key = call_key(call.name, call.input)
        # Denied before, as this exact call or by the same rule: the model is trying again
        # (often the same thing in another form). Counted, and told so more firmly.
        rule_key = f"{decision.rule}|{decision.kind}"
        repeated = key in state.denied_calls or rule_key in state.denied_rules
        state.denied_calls.add(key)
        state.denied_rules.add(rule_key)
        state.policy_denials += 1
        state.repeat_denials += repeated
        await self._sink.emit(
            ToolPolicyEvent(
                **self._meta(run_id),
                step=step,
                tool_use_id=call.id,
                name=call.name,
                action="deny",
                rule=decision.rule,
                reason=decision.reason,
                risk=decision.risk,
                kind=decision.kind,
                network=decision.network,
                effects=decision.to_event()["effects"],
                repeated=repeated,
            )
        )
        text = denial_message(decision, repeated=repeated)
        return _ToolOutcome(ToolResult(text, True, "blocked"), True, 0, 0)


def _describe_rule(r: Rule) -> str:
    if r.command:
        return f"{r.tool}: {r.command}"
    return f"{r.tool}" + (f" ({', '.join(r.effect)})" if r.effect else "")
