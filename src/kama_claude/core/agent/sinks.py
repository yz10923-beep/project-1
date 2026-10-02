"""Where run events go. The loop only knows the EventSink protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Protocol, TextIO

from kama_claude.core.bus.events import (
    Event,
    LLMDeltaEvent,
    LLMResponseEvent,
    NoteUpdatedEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    is_durable,
)
from kama_claude.core.plan import PLAN_TOOL_NAMES, PlanTask, render_task
from kama_claude.core.tools.note_tools import NOTE_TOOL_NAMES


class EventSink(Protocol):
    async def emit(self, event: Event) -> None: ...


class JsonlEventWriter:
    """Append-only events.jsonl. One line per event, flushed immediately, so a crash
    mid-run still leaves every event up to the crash on disk."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh: IO[str] = path.open("a", encoding="utf-8")

    async def emit(self, event: Event) -> None:
        if not is_durable(event):
            return  # streamed text is live-only; llm.response carries the full text
        self._fh.write(event.model_dump_json() + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class FanoutSink:
    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks

    async def emit(self, event: Event) -> None:
        for sink in self._sinks:
            await sink.emit(event)


class ConsolePrinter:
    """Human-readable progress for `kama run`."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._streamed_steps: set[int] = set()
        self._mid_line = False
        self._plan: dict[int, PlanTask] = {}

    async def emit(self, event: Event) -> None:
        if not isinstance(event, LLMDeltaEvent) and self._mid_line:
            self._out.write("\n")
            self._mid_line = False
        match event:
            case LLMDeltaEvent():
                if event.step not in self._streamed_steps:
                    self._streamed_steps.add(event.step)
                    self._out.write("  ")
                self._out.write(event.text.replace("\n", "\n  "))
                self._out.flush()
                self._mid_line = True
            case RunStartedEvent():
                self._p(f"run {event.run_id} · model={event.model} · workspace={event.workspace}")
                if event.session_id:
                    carried = f", continuing {event.history_messages} messages"
                    self._p(
                        f"  session {event.session_id}"
                        + (carried if event.history_messages else " (new)")
                        + (
                            f", repaired {event.repaired} interrupted tool calls"
                            if event.repaired
                            else ""
                        )
                    )
                if event.preamble:
                    notes = event.preamble.count("\n- [")
                    self._p(f"  memory: {notes} note(s) sent before the goal")
            case NoteUpdatedEvent():
                why = f" ({event.reason})" if event.reason else ""
                vol = " [volatile]" if event.note.volatile else ""
                self._p(
                    f"  ✎ note {event.note.id} {event.action}{vol}: "
                    f"{_one_line(event.note.text, 120)}{why}"
                )
            case ToolStartedEvent() if event.name in NOTE_TOOL_NAMES:
                pass  # the note line says what changed
            case ToolFinishedEvent() if event.name in NOTE_TOOL_NAMES and not event.is_error:
                pass
            case ToolApprovalResolvedEvent() if event.by not in ("user", "auto"):
                self._p(f"  ! approval {event.by}: {'approved' if event.approved else 'denied'}")
            case LLMResponseEvent():
                u = event.usage
                self._p(
                    f"[step {event.step}] {event.stop_reason} · {event.latency_ms}ms · "
                    f"in={u.input_tokens} out={u.output_tokens} "
                    f"cache_read={u.cache_read_input_tokens}"
                )
                text = "".join(b.get("text", "") for b in event.content if b.get("type") == "text")
                if (
                    text.strip()
                    and event.stop_reason == "tool_use"
                    and (event.step not in self._streamed_steps)
                ):
                    self._p(f"  {_one_line(text, 200)}")
            case PlanUpdatedEvent():
                if event.by == "user":
                    self._p(f"  ✎ you changed the plan: {event.summary}")
                self._show_plan(event.tasks)
            case PlanNoticeEvent():
                self._p("  (the model has been told about your plan change)")
            case PlanReminderEvent():
                n = len(event.open_task_ids)
                self._p(f"  ! stopped with {n} open task(s); reminding the model of its plan")
            case ToolStartedEvent() if event.name in PLAN_TOOL_NAMES:
                pass  # the plan lines below say what changed
            case ToolFinishedEvent() if event.name in PLAN_TOOL_NAMES and not event.is_error:
                pass
            case ToolStartedEvent():
                self._p(f"  → {event.name} {_one_line(json.dumps(event.input), 160)}")
            case ToolFinishedEvent():
                mark = "denied" if event.denied else ("error" if event.is_error else "ok")
                wait = f" (waited {event.approval_ms / 1000:.1f}s)" if event.approval_ms else ""
                self._p(
                    f"  ← {mark} · {event.duration_ms}ms{wait} · {_one_line(event.output, 160)}"
                )
            case RunFinishedEvent():
                u = event.usage
                self._p(
                    f"\n== {event.status} after {event.steps} steps · {event.duration_ms}ms · "
                    f"tokens in={u.input_tokens} out={u.output_tokens} "
                    f"cache_read={u.cache_read_input_tokens} "
                    f"cache_write={u.cache_creation_input_tokens}"
                )
                if self._plan:
                    self._p(_plan_summary(list(self._plan.values())))
                if event.error:
                    self._p(f"error: {event.error}")
                if event.final_text and not self._streamed_steps:
                    self._p(f"\n{event.final_text}")
            case _:
                pass

    def _show_plan(self, tasks: list[PlanTask]) -> None:
        """Whole checklist when tasks are added; one line per change after that."""
        before = self._plan
        self._plan = {t.id: t for t in tasks}
        progress = f"({sum(t.status == 'completed' for t in tasks)}/{len(tasks)})"
        if self._plan.keys() - before.keys():
            self._p(f"  plan {progress}")
            for t in tasks:
                self._p(f"    {render_task(t, tasks)}")
            return
        for t in tasks:
            if t != before.get(t.id):
                self._p(f"  {render_task(t, tasks)}  {progress}")

    def _p(self, line: str) -> None:
        print(line, file=self._out, flush=True)


def _plan_summary(tasks: list[PlanTask]) -> str:
    n = {s: sum(t.status == s for t in tasks) for s in ("completed", "cancelled")}
    left = len(tasks) - n["completed"] - n["cancelled"]
    return (
        f"plan: {n['completed']}/{len(tasks)} completed · {n['cancelled']} cancelled · {left} open"
    )


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
