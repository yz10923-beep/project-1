"""Events. A discriminated union on `type` so consumers dispatch on one field and
pydantic rejects unknown shapes.

Run events are the single source of truth for what an agent run did: the
transcript, tool I/O, token usage and timings can all be rebuilt from
events.jsonl. S2 streams the same events to clients over IPC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from kama_claude.core.llm.types import StopReason, Usage
from kama_claude.core.notes import Note, NoteAction
from kama_claude.core.outputs import OutputCut
from kama_claude.core.plan import ChangedBy, PlanTask


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    version: str
    listen: str
    at: datetime


class CoreStoppingEvent(BaseModel):
    type: Literal["core.stopping"] = "core.stopping"
    reason: str
    at: datetime


class _RunEvent(BaseModel):
    run_id: str
    seq: int = Field(ge=0, description="Per-run sequence number, strictly increasing from 0.")
    at: datetime


class RunStartedEvent(_RunEvent):
    type: Literal["run.started"] = "run.started"
    goal: str
    model: str
    workspace: str
    max_steps: int
    planning: bool = Field(default=False, description="task_* tools offered (S3+).")
    # S4: a run in a session continues the session's history (rebuilt from earlier runs'
    # events) and may open with a memory preamble (notes) before the goal.
    session_id: str | None = None
    history_messages: int = Field(default=0, description="Messages carried in from the session.")
    repaired: int = Field(default=0, description="Missing tool results added to that history.")
    preamble: str | None = Field(default=None, description="Memory block sent before the goal.")
    # S5: the run's permission mode and sandbox, as they were when it started.
    policy: dict[str, Any] | None = Field(
        default=None, description="mode, sandbox backend, rule counts, warnings; None = off."
    )


class LLMResponseEvent(_RunEvent):
    type: Literal["llm.response"] = "llm.response"
    step: int
    stop_reason: StopReason
    content: list[dict[str, Any]]
    usage: Usage
    latency_ms: int
    model: str = Field(default="", description="Model that served this call, from the response.")
    ttft_ms: int | None = Field(default=None, description="Time to first generated token.")


class ToolStartedEvent(_RunEvent):
    type: Literal["tool.started"] = "tool.started"
    step: int
    tool_use_id: str
    name: str
    input: dict[str, Any]


class ToolApprovalRequestedEvent(_RunEvent):
    type: Literal["tool.approval_requested"] = "tool.approval_requested"
    step: int
    tool_use_id: str
    name: str
    input: dict[str, Any]
    # S5: why the policy asks, so a client can show the risk (empty with policy off).
    risk: str = ""
    reason: str = ""
    rule: str = ""
    remember: list[str] = Field(
        default_factory=list, description="What 'always allow' would add for this session."
    )


class ToolPolicyEvent(_RunEvent):
    """The policy decided a tool call without asking anyone (S5): a deny (the call did not
    run) or an allow of something beyond reading."""

    type: Literal["tool.policy"] = "tool.policy"
    step: int
    tool_use_id: str
    name: str
    action: Literal["allow", "deny"]
    rule: str
    reason: str
    risk: str
    kind: str
    network: bool = False
    effects: list[str] = Field(default_factory=list)
    repeated: bool = Field(
        default=False,
        description="A deny after an earlier deny by the same rule or of the "
        "same call: the model is trying again, in another form or not.",
    )


class ToolApprovalResolvedEvent(_RunEvent):
    type: Literal["tool.approval_resolved"] = "tool.approval_resolved"
    step: int
    tool_use_id: str
    approved: bool
    by: str = Field(description="user | auto | timeout | ...: who or what decided.")
    reason: str = Field(default="", description="The user's reason for a denial (S5).")
    remembered: list[str] = Field(
        default_factory=list, description="Rules added for the session by 'always allow'."
    )


class LLMDeltaEvent(BaseModel):
    """Streamed model text. Ephemeral: broadcast to live clients, never persisted or
    replayed (llm.response carries the full text), so it has no seq."""

    type: Literal["llm.delta"] = "llm.delta"
    run_id: str
    step: int
    text: str


class ToolFinishedEvent(_RunEvent):
    type: Literal["tool.finished"] = "tool.finished"
    step: int
    tool_use_id: str
    name: str
    is_error: bool
    denied: bool = False
    output: str
    duration_ms: int = Field(description="Tool execution time only; 0 if denied.")
    approval_ms: int = Field(default=0, description="Time spent waiting for the user.")
    error_kind: str | None = Field(
        default=None,
        description="invalid_input | unknown_tool | not_found | blocked | denied | timeout | "
        "crashed | ... (S5); None when the call succeeded.",
    )
    cut: OutputCut | None = Field(
        default=None,
        description="S6: the result was over the cap; `output` is what the model saw, and the "
        "whole text was saved under output_id for read_output.",
    )


class LLMRetryEvent(_RunEvent):
    """A model call failed with a retryable error and will be tried again after `wait_s`
    (S5). Text streamed during the failed attempt is void: clients drop it."""

    type: Literal["llm.retry"] = "llm.retry"
    step: int
    attempt: int = Field(description="The attempt that failed, from 1.")
    kind: str
    error: str
    wait_s: float
    status: int | None = None


class PlanUpdatedEvent(_RunEvent):
    """The whole plan after a task_* call changed it. A snapshot, not a diff: a client
    that attaches late needs only the latest one, and replaying one twice is harmless."""

    type: Literal["plan.updated"] = "plan.updated"
    step: int
    tool_use_id: str | None = Field(description="The task_* call; None for a user edit.")
    tasks: list[PlanTask]
    by: ChangedBy = "model"
    summary: str = Field(default="", description="What changed, for user edits.")


class NoteUpdatedEvent(_RunEvent):
    """A durable note was added, changed or deleted during this run (S4 memory)."""

    type: Literal["note.updated"] = "note.updated"
    step: int
    tool_use_id: str | None = Field(description="The note_* call; None for a user edit.")
    action: NoteAction
    note: Note
    reason: str = ""


class PlanNoticeEvent(_RunEvent):
    """User plan edits, delivered to the model: `text` was appended to the user message
    sent at `step`. Durable for the same reason as plan.reminder: it is conversation."""

    type: Literal["plan.notice"] = "plan.notice"
    step: int
    text: str


class PlanReminderEvent(_RunEvent):
    """The model ended its turn with open tasks, so the loop sent `text` back as a user
    message instead of finishing (once per run). Durable, because it is part of the
    conversation: without it events.jsonl could not reconstruct what the model saw."""

    type: Literal["plan.reminder"] = "plan.reminder"
    step: int
    open_task_ids: list[int]
    text: str


RunStatus = Literal[
    "completed",
    "max_steps",
    "truncated",
    "refused",
    "error",
    "cancelled",
    "context_overflow",  # S6: the request outgrew the model's window (the agent's doing)
]


class RunFinishedEvent(_RunEvent):
    type: Literal["run.finished"] = "run.finished"
    status: RunStatus
    final_text: str
    steps: int
    usage: Usage
    duration_ms: int
    error: str | None = None
    retryable: bool | None = None
    plan_only_steps: int = Field(
        default=0, description="Steps whose only tool calls were plan tools (S3)."
    )
    budget_credit: int = Field(
        default=0, description="Plan-only steps not counted against max_steps."
    )
    # S5 counters: what the policy and the retries did during the run.
    policy_denials: int = 0
    repeat_denials: int = Field(default=0, description="Denials of something already denied.")
    approvals_asked: int = 0
    llm_retries: int = 0
    tool_errors: dict[str, int] = Field(default_factory=dict, description="By error kind.")
    # S6: the largest request the run sent, in tokens (input + cache read + cache write),
    # and how many times the history was compacted.
    context_peak: int = 0
    compactions: int = 0


class ContextCompactedEvent(_RunEvent):
    """S6: the history outgrew the budget and was summarized server-side (on-demand
    compaction). From here the conversation is [assistant: block] + the `resume` user
    turn, then whatever follows; replay rebuilds exactly that. The block is stored as
    the API returned it: its signature must reach the API unchanged."""

    type: Literal["context.compacted"] = "context.compacted"
    step: int
    block: dict[str, Any]
    resume: str = Field(description="The user turn after the block: goal and plan, re-stated.")
    tokens_before: int
    measured: Literal["estimate", "count"]
    messages_replaced: int
    usage: Usage
    latency_ms: int
    model: str = ""


class ContextCompactionFailedEvent(_RunEvent):
    """S6: a compaction was needed but no summary came back (cut off, refused, an API
    error after retries). The run continues on the full history and tries again later."""

    type: Literal["context.compaction_failed"] = "context.compaction_failed"
    step: int
    tokens_before: int
    reason: str
    usage: Usage = Field(default_factory=Usage)


Event = Annotated[
    CoreStartedEvent
    | CoreStoppingEvent
    | RunStartedEvent
    | LLMResponseEvent
    | ToolStartedEvent
    | ToolApprovalRequestedEvent
    | ToolApprovalResolvedEvent
    | ToolPolicyEvent
    | ToolFinishedEvent
    | LLMRetryEvent
    | PlanUpdatedEvent
    | PlanReminderEvent
    | PlanNoticeEvent
    | NoteUpdatedEvent
    | ContextCompactedEvent
    | ContextCompactionFailedEvent
    | RunFinishedEvent
    | LLMDeltaEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def is_durable(event: Event) -> bool:
    """Durable events are persisted and replayable; ephemeral ones are live-only."""
    return not isinstance(event, LLMDeltaEvent)
