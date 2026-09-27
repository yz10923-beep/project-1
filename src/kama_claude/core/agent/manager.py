"""RunManager: the daemon side of S2. Owns every run, persists and fans out its events,
and routes approval requests to whichever client answers first.

Design rules:
- A run never waits for a client. Each subscriber has a bounded queue; one that falls
  behind is cut off with a `lagged` stream end carrying the seq to resume from.
- Replay + live with no gap and no duplicate: the backlog snapshot and the subscriber
  registration happen with no await in between, and the event loop is single-threaded.
- A run outlives its clients. A disconnect ends the subscription, never the run; only
  run.cancel (or daemon shutdown) stops it.

Tracing: every run gets a tracer writing <run_dir>/trace.jsonl, and every subscription
is recorded there as a `bus.subscribe` span with its delivery lag.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anthropic

from kama_claude.core.agent.loop import ApprovalDecision, RunResult
from kama_claude.core.agent.runner import (
    build_loop,
    make_provider,
    new_run_id,
    run_tracer,
    runs_root,
)
from kama_claude.core.agent.sinks import JsonlEventWriter
from kama_claude.core.bus.commands import RunInfo, StreamEnd
from kama_claude.core.bus.events import (
    EVENT_ADAPTER,
    Event,
    RunFinishedEvent,
    is_durable,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMProvider, ToolCall
from kama_claude.core.trace.tracer import Tracer

logger = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_SIZE = 1000

type SendEvent = Callable[[Event], Awaitable[None]]
type SendEnd = Callable[[StreamEnd], Awaitable[None]]


class UnknownRun(Exception):
    pass


class _Lagged:
    """Queue marker: this subscriber fell behind and was cut off."""


@dataclass(eq=False)  # identity equality, so instances are hashable and can live in a set
class _Subscriber:
    # Each item carries the monotonic time it was enqueued, to measure delivery lag.
    queue: asyncio.Queue[tuple[Event, int] | _Lagged] = field(
        default_factory=lambda: asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
    )

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait((event, time.perf_counter_ns()))
        except asyncio.QueueFull:
            # Drop everything queued and leave only the marker: the client resumes from
            # its last seq via replay, which is cheaper than making the run wait.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(_Lagged())


@dataclass
class _DeliveryStats:
    """Delivery lag = time from the run emitting an event to it being written to this
    client's socket. Recorded once per subscription as a `bus.subscribe` span."""

    from_seq: int
    replayed: int
    start_ns: int = field(default_factory=time.time_ns)
    t0: int = field(default_factory=time.perf_counter_ns)
    live_events: int = 0
    deltas: int = 0
    lag_total_ns: int = 0
    lag_max_ns: int = 0

    def add(self, event: Event, lag_ns: int) -> None:
        self.live_events += 1
        self.deltas += event.type == "llm.delta"
        self.lag_total_ns += lag_ns
        self.lag_max_ns = max(self.lag_max_ns, lag_ns)

    def attrs(self) -> dict[str, object]:
        mean = self.lag_total_ns / self.live_events if self.live_events else 0
        return {
            "from_seq": self.from_seq,
            "replayed": self.replayed,
            "live_events": self.live_events,
            "deltas": self.deltas,
            "lag_mean_ms": round(mean / 1e6, 3),
            "lag_max_ms": round(self.lag_max_ns / 1e6, 3),
        }


@dataclass
class RunHandle:
    run_id: str
    goal: str
    workspace: Path
    run_dir: Path
    auto_approve: bool
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: str = "running"
    history: list[Event] = field(default_factory=list)
    subscribers: set[_Subscriber] = field(default_factory=set)
    pending: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    task: asyncio.Task[RunResult] | None = None
    tracer: Tracer = field(default_factory=Tracer.noop)

    def info(self) -> RunInfo:
        return RunInfo(
            run_id=self.run_id,
            status=self.status,
            goal=self.goal,
            workspace=str(self.workspace),
            started_at=self.started_at,
            pending_approvals=len(self.pending),
        )


class _RunSink:
    """Persist durable events, keep them for replay, and fan every event out."""

    def __init__(self, handle: RunHandle, writer: JsonlEventWriter) -> None:
        self._handle = handle
        self._writer = writer

    async def emit(self, event: Event) -> None:
        if is_durable(event):
            await self._writer.emit(event)
            self._handle.history.append(event)
        for sub in list(self._handle.subscribers):
            sub.offer(event)


class RunManager:
    def __init__(
        self,
        settings: Settings,
        provider_factory: Callable[[Settings], LLMProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._provider_factory = provider_factory or self._shared_client_provider
        self.runs: dict[str, RunHandle] = {}
        # One SDK client per API key for the daemon's lifetime. Building a client costs
        # ~50-80ms (TLS setup), and a shared one keeps its connection pool, so later runs
        # skip the TLS handshake too. Found via the trace: run.start took 83ms.
        self._clients: dict[str | None, anthropic.AsyncAnthropic] = {}

    def warm_up(self) -> None:
        """Build the default SDK client at daemon start, not during the first run.start."""
        if self._provider_factory == self._shared_client_provider:
            self._shared_client_provider(self._settings)

    def _shared_client_provider(self, settings: Settings) -> LLMProvider:
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        if key not in self._clients:
            self._clients[key] = anthropic.AsyncAnthropic(api_key=key)
        return make_provider(settings, client=self._clients[key])

    def start(
        self,
        goal: str,
        workspace: Path,
        *,
        auto_approve: bool,
        model: str | None = None,
        max_steps: int | None = None,
    ) -> RunHandle:
        overrides = {k: v for k, v in {"model": model, "max_steps": max_steps}.items() if v}
        settings = self._settings.model_copy(update=overrides)
        run_id = new_run_id()
        run_dir = runs_root(settings, workspace) / run_id
        handle = RunHandle(run_id, goal, workspace, run_dir, auto_approve)
        handle.tracer = run_tracer(run_id, run_dir)
        writer = JsonlEventWriter(run_dir / "events.jsonl")

        async def approve(call: ToolCall) -> ApprovalDecision:
            return await self._await_approval(handle, call)

        loop = build_loop(
            settings,
            workspace=workspace,
            sink=_RunSink(handle, writer),
            approver=approve,
            provider=self._provider_factory(settings),
            tracer=handle.tracer,
        )

        async def drive() -> RunResult:
            try:
                result = await loop.run(goal, run_id)
                handle.status = result.status
                return result
            except asyncio.CancelledError:
                handle.status = "cancelled"
                raise
            finally:
                writer.close()
                for fut in handle.pending.values():
                    fut.cancel()

        handle.task = asyncio.create_task(drive(), name=f"run-{run_id}")
        self.runs[run_id] = handle
        return handle

    async def _await_approval(self, handle: RunHandle, call: ToolCall) -> ApprovalDecision:
        if handle.auto_approve:
            return ApprovalDecision(True, "auto")
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        handle.pending[call.id] = fut
        try:
            approved = await asyncio.wait_for(fut, timeout=self._settings.approval_timeout_s)
            return ApprovalDecision(approved, "user")
        except TimeoutError:
            return ApprovalDecision(False, "timeout")
        finally:
            handle.pending.pop(call.id, None)

    def respond(self, run_id: str, tool_use_id: str, approve: bool) -> bool:
        """Answer a pending approval. False if unknown, already answered, or expired."""
        handle = self._get(run_id)
        fut = handle.pending.get(tool_use_id)
        if fut is None or fut.done():
            return False
        fut.set_result(approve)
        return True

    def cancel(self, run_id: str) -> bool:
        handle = self._get(run_id)
        if handle.task is None or handle.task.done():
            return False
        handle.task.cancel()
        return True

    def tracer_for(self, run_id: str) -> Tracer | None:
        handle = self.runs.get(run_id)
        return handle.tracer if handle else None

    async def subscribe(
        self, run_id: str, from_seq: int, send: SendEvent, end: SendEnd, client: str = "?"
    ) -> None:
        """Stream a run's events to one client: stored backlog from `from_seq`, then live
        events until run.finished. Returns when the subscription ends."""
        handle = self.runs.get(run_id)
        if handle is None:
            await self._replay_from_disk(run_id, from_seq, send, end)
            return
        # No await between the snapshot and the registration: nothing can slip between.
        backlog = [e for e in handle.history if e.seq >= from_seq]  # type: ignore[union-attr]
        # run.finished may already be recorded while the run task is still unwinding.
        finished = (handle.task is not None and handle.task.done()) or any(
            isinstance(e, RunFinishedEvent) for e in handle.history
        )
        sub = _Subscriber()
        if not finished:
            handle.subscribers.add(sub)
        next_seq = from_seq
        stats = _DeliveryStats(from_seq=from_seq, replayed=len(backlog))
        ended = "disconnected"
        try:
            for event in backlog:
                await send(event)
                next_seq = event.seq + 1  # type: ignore[union-attr]
            if finished:
                ended = "finished"
                await end(StreamEnd(run_id=run_id, reason="finished", next_seq=next_seq))
                return
            while True:
                item = await sub.queue.get()
                if isinstance(item, _Lagged):
                    ended = "lagged"
                    await end(StreamEnd(run_id=run_id, reason="lagged", next_seq=next_seq))
                    return
                event, enqueued_ns = item
                if is_durable(event):
                    if event.seq < next_seq:  # type: ignore[union-attr]
                        continue  # already sent in the backlog
                    next_seq = event.seq + 1  # type: ignore[union-attr]
                await send(event)
                stats.add(event, time.perf_counter_ns() - enqueued_ns)
                if isinstance(event, RunFinishedEvent):
                    ended = "finished"
                    await end(StreamEnd(run_id=run_id, reason="finished", next_seq=next_seq))
                    return
        finally:
            handle.subscribers.discard(sub)
            handle.tracer.record(
                "bus.subscribe",
                "bus",
                start_ns=stats.start_ns,
                duration_ns=time.perf_counter_ns() - stats.t0,
                status="error" if ended == "lagged" else "ok",
                error="subscriber fell behind and was cut off" if ended == "lagged" else None,
                attrs={"client": client, "ended": ended, **stats.attrs()},
            )

    async def _replay_from_disk(
        self, run_id: str, from_seq: int, send: SendEvent, end: SendEnd
    ) -> None:
        path = self._find_run_file(run_id, "events.jsonl")
        if path is None:
            raise UnknownRun(run_id)
        next_seq = from_seq
        for line in path.read_text().splitlines():
            event = EVENT_ADAPTER.validate_json(line)
            if event.seq >= from_seq:  # type: ignore[union-attr]
                await send(event)
                next_seq = event.seq + 1  # type: ignore[union-attr]
        await end(StreamEnd(run_id=run_id, reason="replayed", next_seq=next_seq))

    def _find_run_file(self, run_id: str, name: str) -> Path | None:
        if "/" in run_id or ".." in run_id:
            return None
        root = self._settings.runs_dir.expanduser()
        path = root / run_id / name
        return path if root.is_absolute() and path.is_file() else None

    def exists(self, run_id: str) -> bool:
        return run_id in self.runs or self._find_run_file(run_id, "events.jsonl") is not None

    def _get(self, run_id: str) -> RunHandle:
        handle = self.runs.get(run_id)
        if handle is None:
            raise UnknownRun(run_id)
        return handle

    async def shutdown(self) -> None:
        """Cancel live runs and wait for each to record run.finished."""
        tasks = [h.task for h in self.runs.values() if h.task and not h.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
