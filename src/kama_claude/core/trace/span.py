from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# The layer a span belongs to: model calls, the agent loop, tool execution, event
# delivery to clients, and IPC requests.
SpanKind = Literal["agent", "llm", "tool", "bus", "ipc"]
SpanStatus = Literal["ok", "error", "cancelled"]


class Span(BaseModel):
    trace_id: str  # the run id (or "daemon" for requests not tied to a run)
    span_id: str
    parent_id: str | None = None
    name: str
    kind: SpanKind
    start_ns: int = Field(description="Wall-clock start, ns since the Unix epoch.")
    duration_ns: int = Field(ge=0, description="Measured on a monotonic clock.")
    status: SpanStatus = "ok"
    error: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)

    @property
    def end_ns(self) -> int:
        return self.start_ns + self.duration_ns

    @property
    def duration_ms(self) -> float:
        return self.duration_ns / 1e6
