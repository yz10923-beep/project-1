"""What the TUI knows about one run, folded from its events. Pure: no widgets, no IO,
so the bookkeeping (dedupe on reconnect, cost, plan, pending approvals) is unit-tested
without a terminal. The app renders from this and appends to its log."""

from __future__ import annotations

from dataclasses import dataclass, field

from kama_claude.core.bus.events import (
    Event,
    LLMResponseEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    is_durable,
)
from kama_claude.core.llm.pricing import cost_usd
from kama_claude.core.llm.types import Usage
from kama_claude.core.plan import PlanTask


@dataclass
class RunView:
    run_id: str
    goal: str = ""
    model: str = ""
    workspace: str = ""
    status: str = "starting"  # running, then a RunStatus
    step: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = 0.0
    planning: bool = False
    plan: list[PlanTask] = field(default_factory=list)
    pending: dict[str, ToolApprovalRequestedEvent] = field(default_factory=dict)
    next_seq: int = 0  # resume point after a disconnect or a lagged stream
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.status not in ("starting", "running")

    def apply(self, event: Event) -> bool:
        """Fold one event in. False if it was already seen (a replay after reconnect
        overlaps what was delivered live), in which case the caller must not render it."""
        if is_durable(event):
            seq = event.seq  # type: ignore[union-attr]
            if seq < self.next_seq:
                return False
            self.next_seq = seq + 1
        match event:
            case RunStartedEvent():
                self.goal, self.model, self.workspace = event.goal, event.model, event.workspace
                self.planning, self.status = event.planning, "running"
            case LLMResponseEvent():
                self.step = event.step
                self.usage = self.usage + event.usage
                cost = cost_usd(event.model or self.model, event.usage.model_dump())
                self.cost_usd = (
                    None if cost is None or self.cost_usd is None else self.cost_usd + cost
                )
            case PlanUpdatedEvent():
                self.plan = event.tasks
            case ToolApprovalRequestedEvent():
                self.pending[event.tool_use_id] = event
            case ToolApprovalResolvedEvent():
                self.pending.pop(event.tool_use_id, None)
            case RunFinishedEvent():
                self.status, self.error = event.status, event.error
                self.pending.clear()
            case _:
                pass
        return True

    def headline(self, *, auto_approve: bool, connection: str) -> str:
        cost = f"${self.cost_usd:.4f}" if self.cost_usd is not None else "cost unknown"
        u = self.usage
        parts = [
            self.run_id,
            self.status,
            f"step {self.step}",
            f"in {u.input_tokens + u.cache_read_input_tokens + u.cache_creation_input_tokens:,}"
            f" out {u.output_tokens:,}",
            cost,
        ]
        if self.plan:
            done = sum(t.status == "completed" for t in self.plan)
            parts.append(f"plan {done}/{len(self.plan)}")
        if self.pending:
            parts.append(f"{len(self.pending)} awaiting approval")
        parts.append("auto-approve ON" if auto_approve else "asks before bash/write")
        parts.append(connection)
        return " · ".join(parts)
