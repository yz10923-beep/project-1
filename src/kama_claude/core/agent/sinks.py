"""Where run events go. The loop only knows the EventSink protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, Protocol, TextIO

from kama_claude.core.bus.events import (
    Event,
    LLMResponseEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
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

    async def emit(self, event: Event) -> None:
        match event:
            case RunStartedEvent():
                self._p(f"run {event.run_id} · model={event.model} · workspace={event.workspace}")
            case LLMResponseEvent():
                u = event.usage
                self._p(
                    f"[step {event.step}] {event.stop_reason} · {event.latency_ms}ms · "
                    f"in={u.input_tokens} out={u.output_tokens} "
                    f"cache_read={u.cache_read_input_tokens}"
                )
                text = "".join(b.get("text", "") for b in event.content if b.get("type") == "text")
                if text.strip() and event.stop_reason == "tool_use":
                    self._p(f"  {_one_line(text, 200)}")
            case ToolStartedEvent():
                self._p(f"  → {event.name} {_one_line(json.dumps(event.input), 160)}")
            case ToolFinishedEvent():
                mark = "denied" if event.denied else ("error" if event.is_error else "ok")
                self._p(f"  ← {mark} · {event.duration_ms}ms · {_one_line(event.output, 160)}")
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
                if event.final_text:
                    self._p(f"\n{event.final_text}")
            case _:
                pass

    def _p(self, line: str) -> None:
        print(line, file=self._out, flush=True)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
