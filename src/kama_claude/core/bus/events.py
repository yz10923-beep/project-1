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


class LLMResponseEvent(_RunEvent):
    type: Literal["llm.response"] = "llm.response"
    step: int
    stop_reason: StopReason
    content: list[dict[str, Any]]
    usage: Usage
    latency_ms: int
    model: str = Field(default="", description="Model that served this call, from the response.")


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


class ToolApprovalResolvedEvent(_RunEvent):
    type: Literal["tool.approval_resolved"] = "tool.approval_resolved"
    step: int
    tool_use_id: str
    approved: bool
    by: str = Field(description="user | auto | timeout | ...: who or what decided.")


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


RunStatus = Literal["completed", "max_steps", "truncated", "refused", "error", "cancelled"]


class RunFinishedEvent(_RunEvent):
    type: Literal["run.finished"] = "run.finished"
    status: RunStatus
    final_text: str
    steps: int
    usage: Usage
    duration_ms: int
    error: str | None = None
    retryable: bool | None = None


Event = Annotated[
    CoreStartedEvent
    | CoreStoppingEvent
    | RunStartedEvent
    | LLMResponseEvent
    | ToolStartedEvent
    | ToolApprovalRequestedEvent
    | ToolApprovalResolvedEvent
    | ToolFinishedEvent
    | RunFinishedEvent
    | LLMDeltaEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def is_durable(event: Event) -> bool:
    """Durable events are persisted and replayable; ephemeral ones are live-only."""
    return not isinstance(event, LLMDeltaEvent)
