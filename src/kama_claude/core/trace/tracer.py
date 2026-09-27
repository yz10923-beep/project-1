"""Creates spans and links each one to its parent.

The current span lives in a ContextVar, not a global: every asyncio task gets its own
copy of the context, so concurrent tasks never become each other's parents, and a task
created inside a span still sees that span as its parent.

Timing: the start is wall-clock (to place spans on a timeline), the duration comes from
a monotonic clock (wall-clock can jump when NTP adjusts it).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol

from kama_claude.core.trace.span import Span, SpanKind, SpanStatus


class SpanSink(Protocol):
    def write(self, span: Span) -> None: ...


class JsonlSpanWriter:
    """Appends one span per line. Opens the file per write: spans are few (tens per run)
    and may arrive after the run ended (e.g. a subscriber disconnecting later)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, span: Span) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(span.model_dump_json() + "\n")


class ActiveSpan:
    """A span in progress. Set attributes while it runs; it is written when it ends."""

    def __init__(
        self,
        tracer: Tracer,
        name: str,
        kind: SpanKind,
        parent_id: str | None,
        attrs: dict[str, Any],
    ) -> None:
        self.tracer = tracer
        self.span_id = secrets.token_hex(8)
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.attrs = attrs
        self.status: SpanStatus = "ok"
        self.error: str | None = None
        self._start_ns = time.time_ns()
        self._t0 = time.perf_counter_ns()

    def set(self, **attrs: Any) -> None:
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})

    def fail(self, error: str, status: SpanStatus = "error") -> None:
        self.status, self.error = status, error

    def elapsed_ns(self) -> int:
        return time.perf_counter_ns() - self._t0

    def end(self) -> Span:
        span = Span(
            trace_id=self.tracer.trace_id,
            span_id=self.span_id,
            parent_id=self.parent_id,
            name=self.name,
            kind=self.kind,
            start_ns=self._start_ns,
            duration_ns=self.elapsed_ns(),
            status=self.status,
            error=self.error,
            attrs=self.attrs,
        )
        self.tracer.emit(span)
        return span


_current: ContextVar[ActiveSpan | None] = ContextVar("kama_current_span", default=None)


class Tracer:
    def __init__(self, trace_id: str, sink: SpanSink | None = None) -> None:
        self.trace_id = trace_id
        self._sink = sink

    @classmethod
    def noop(cls) -> Tracer:
        return cls("noop", None)

    def emit(self, span: Span) -> None:
        if self._sink is not None:
            self._sink.write(span)

    def _parent(self) -> tuple[str | None, dict[str, Any]]:
        parent = _current.get()
        if parent is None:
            return None, {}
        if parent.tracer.trace_id != self.trace_id:
            # e.g. a run started from inside an IPC request span: a different trace. Keep
            # the causal link as an attribute instead of a parent that isn't in this file.
            return None, {"linked_span": f"{parent.tracer.trace_id}/{parent.span_id}"}
        return parent.span_id, {}

    @contextlib.contextmanager
    def span(self, name: str, kind: SpanKind, **attrs: Any) -> Iterator[ActiveSpan]:
        """Time the `with` block as a child of the current span."""
        parent_id, link = self._parent()
        active = ActiveSpan(self, name, kind, parent_id, {**link, **attrs})
        token = _current.set(active)
        try:
            yield active
        except asyncio.CancelledError:
            active.fail("cancelled", "cancelled")
            raise
        except BaseException as e:
            if active.status == "ok":
                active.fail(f"{type(e).__name__}: {e}")
            raise
        finally:
            _current.reset(token)
            active.end()

    def record(
        self,
        name: str,
        kind: SpanKind,
        *,
        start_ns: int,
        duration_ns: int,
        parent_id: str | None = None,
        status: SpanStatus = "ok",
        error: str | None = None,
        attrs: Mapping[str, Any] | None = None,
    ) -> Span:
        """Write a span measured elsewhere (e.g. by the RPC server or a subscriber)."""
        span = Span(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),
            parent_id=parent_id,
            name=name,
            kind=kind,
            start_ns=start_ns,
            duration_ns=max(0, duration_ns),
            status=status,
            error=error,
            attrs=dict(attrs or {}),
        )
        self.emit(span)
        return span
