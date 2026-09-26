"""RunManager: the daemon side of S2. Owns every run, persists and fans out its events,
and routes approval requests to whichever client answers first.

Design rules:
- A run never waits for a client. Each subscriber has a bounded queue; one that falls
  behind is cut off with a `lagged` stream end carrying the seq to resume from.
- Replay + live with no gap and no duplicate: the backlog snapshot and the subscriber
  registration happen with no await in between, and the event loop is single-threaded.
- A run outlives its clients. A disconnect ends the subscription, never the run; only
  run.cancel (or daemon shutdown) stops it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.agent.loop import ApprovalDecision, RunResult
from kama_claude.core.agent.runner import build_loop, new_run_id, runs_root
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
    queue: asyncio.Queue[Event | _Lagged] = field(
        default_factory=lambda: asyncio.Queue(SUBSCRIBER_QUEUE_SIZE)
    )
    last_seq: int = -1

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop everything queued and leave only the marker: the client resumes from
            # its last seq via replay, which is cheaper than making the run wait.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(_Lagged())


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
        self._provider_factory = provider_factory
        self.runs: dict[str, RunHandle] = {}

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
        writer = JsonlEventWriter(run_dir / "events.jsonl")

        async def approve(call: ToolCall) -> ApprovalDecision:
            return await self._await_approval(handle, call)

        loop = build_loop(
            settings,
            workspace=workspace,
            sink=_RunSink(handle, writer),
            approver=approve,
            provider=self._provider_factory(settings) if self._provider_factory else None,
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

    async def subscribe(self, run_id: str, from_seq: int, send: SendEvent, end: SendEnd) -> None:
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
        try:
            for event in backlog:
                await send(event)
                next_seq = event.seq + 1  # type: ignore[union-attr]
            if finished:
                await end(StreamEnd(run_id=run_id, reason="finished", next_seq=next_seq))
                return
            while True:
                item = await sub.queue.get()
                if isinstance(item, _Lagged):
                    await end(StreamEnd(run_id=run_id, reason="lagged", next_seq=next_seq))
                    return
                if is_durable(item):
                    if item.seq < next_seq:  # type: ignore[union-attr]
                        continue  # already sent in the backlog
                    next_seq = item.seq + 1  # type: ignore[union-attr]
                await send(item)
                if isinstance(item, RunFinishedEvent):
                    await end(StreamEnd(run_id=run_id, reason="finished", next_seq=next_seq))
                    return
        finally:
            handle.subscribers.discard(sub)

    async def _replay_from_disk(
        self, run_id: str, from_seq: int, send: SendEvent, end: SendEnd
    ) -> None:
        path = self._find_events_file(run_id)
        if path is None:
            raise UnknownRun(run_id)
        next_seq = from_seq
        for line in path.read_text().splitlines():
            event = EVENT_ADAPTER.validate_json(line)
            if event.seq >= from_seq:  # type: ignore[union-attr]
                await send(event)
                next_seq = event.seq + 1  # type: ignore[union-attr]
        await end(StreamEnd(run_id=run_id, reason="replayed", next_seq=next_seq))

    def _find_events_file(self, run_id: str) -> Path | None:
        if "/" in run_id or ".." in run_id:
            return None
        root = self._settings.runs_dir.expanduser()
        path = root / run_id / "events.jsonl"
        return path if root.is_absolute() and path.is_file() else None

    def exists(self, run_id: str) -> bool:
        return run_id in self.runs or self._find_events_file(run_id) is not None

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
