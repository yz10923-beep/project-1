"""What the TUI knows about one run, folded from its events. Pure: no widgets, no IO,
so the bookkeeping (dedupe on reconnect, cost, plan, pending approvals) is unit-tested
without a terminal. The app renders from this and appends to its log."""

from __future__ import annotations

from dataclasses import dataclass, field

from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    Event,
    LLMResponseEvent,
    LLMRetryEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolPolicyEvent,
    is_durable,
)
from kama_claude.core.context import request_tokens
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
    mode: str | None = None  # the permission mode (S5); None = policy off
    blocked: int = 0
    retries: int = 0
    # S6: the last request's size against the budget (None = context governance off)
    context: int = 0
    context_budget: int | None = None
    compactions: int = 0

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
                self.mode = event.policy["mode"] if event.policy else None
                self.context_budget = event.context["budget"] if event.context else None
            case ToolPolicyEvent(action="deny"):
                self.blocked += 1
            case LLMRetryEvent():
                self.retries += 1
            case LLMResponseEvent():
                self.step = event.step
                self.context = request_tokens(event.usage)
                self._bill(event.model, event.usage)
            case ContextCompactedEvent():
                self.compactions += 1
                self._bill(event.model, event.usage)
            case ContextCompactionFailedEvent():
                self._bill("", event.usage)
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

    def _bill(self, model: str, usage: Usage) -> None:
        """Every call the run paid for, compaction summaries included."""
        self.usage = self.usage + usage
        cost = cost_usd(model or self.model, usage.model_dump())
        self.cost_usd = None if cost is None or self.cost_usd is None else self.cost_usd + cost

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
        if self.context:
            of = f"/{self.context_budget / 1000:.0f}K" if self.context_budget else ""
            comp = f" · {self.compactions} compaction(s)" if self.compactions else ""
            parts.append(f"ctx {self.context / 1000:.1f}K{of}{comp}")
        if self.plan:
            done = sum(t.status == "completed" for t in self.plan)
            parts.append(f"plan {done}/{len(self.plan)}")
        if self.pending:
            parts.append(f"{len(self.pending)} awaiting approval")
        if self.blocked:
            parts.append(f"{self.blocked} blocked")
        if self.retries:
            parts.append(f"{self.retries} model retries")
        if self.mode is not None:
            parts.append(f"mode {self.mode}")
        else:
            parts.append("auto-approve ON" if auto_approve else "asks before bash/write")
        parts.append(connection)
        return " · ".join(parts)
