"""Where run events go. The loop only knows the EventSink protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Protocol, TextIO

from kama_claude.core.bus.events import (
    Event,
    LLMDeltaEvent,
    LLMResponseEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
    is_durable,
)


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
                if event.error:
                    self._p(f"error: {event.error}")
                if event.final_text and not self._streamed_steps:
                    self._p(f"\n{event.final_text}")
            case _:
                pass

    def _p(self, line: str) -> None:
        print(line, file=self._out, flush=True)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
