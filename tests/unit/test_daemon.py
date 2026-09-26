"""S2 done criteria, tested against an in-process kama-core over real sockets:
runs execute in the daemon, several clients watch the same run, late clients get a
replay, approvals round-trip over IPC, and a client going away never stops a run."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.agent import manager as manager_mod
from kama_claude.core.app import CoreApp
from kama_claude.core.bus.commands import (
    APPROVAL_RESPOND,
    EVENT_NOTIFICATION,
    RUN_CANCEL,
    RUN_LIST,
    RUN_START,
    RUN_SUBSCRIBE,
    STREAM_END_NOTIFICATION,
    ApprovalRespondParams,
    ApprovalRespondResult,
    RunCancelParams,
    RunCancelResult,
    RunListParams,
    RunListResult,
    RunStartParams,
    RunStartResult,
    RunSubscribeParams,
    RunSubscribeResult,
    StreamEnd,
)
from kama_claude.core.bus.envelope import INVALID_PARAMS
from kama_claude.core.bus.events import EVENT_ADAPTER, Event
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMProvider
from kama_claude.core.transport.client import JsonRpcClient, RpcError
from tests.fakes import GatedProvider, ScriptedProvider, text_response, tool_response


@dataclass
class Daemon:
    app: CoreApp
    port: int
    ws: Path
    providers: list[ScriptedProvider] = field(default_factory=list)

    def client(self, name: str = "test") -> JsonRpcClient:
        return JsonRpcClient("127.0.0.1", self.port, token=self.app.token, client_name=name)


Script = Callable[[], ScriptedProvider]


@pytest.fixture
async def daemon_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    apps: list[CoreApp] = []

    async def make(script: Script, **settings_kw: Any) -> Daemon:
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        settings = Settings(port=0, runs_dir=tmp_path / "runs", **settings_kw)
        providers: list[ScriptedProvider] = []

        def factory(_: Settings) -> LLMProvider:
            providers.append(script())
            return providers[-1]

        app = CoreApp(settings, provider_factory=factory)
        _, port = await app.server.start()
        apps.append(app)
        return Daemon(app, port, ws, providers)

    yield make
    for app in apps:
        await app.runs.shutdown()
        await app.server.stop()


async def start(c: JsonRpcClient, d: Daemon, auto: bool = True) -> str:
    res = await c.call(
        RUN_START, RunStartParams(goal="g", workspace=str(d.ws), auto_approve=auto), RunStartResult
    )
    return res.run_id


async def collect(
    c: JsonRpcClient, run_id: str, from_seq: int = 0, *, until_end: bool = True
) -> tuple[list[Event], StreamEnd | None]:
    """Subscribe and gather events until the stream ends."""
    await c.call(
        RUN_SUBSCRIBE, RunSubscribeParams(run_id=run_id, from_seq=from_seq), RunSubscribeResult
    )
    events: list[Event] = []
    async for note in c.notifications():
        if note.method == EVENT_NOTIFICATION:
            events.append(EVENT_ADAPTER.validate_python(note.params["event"]))
        elif note.method == STREAM_END_NOTIFICATION:
            return events, StreamEnd.model_validate(note.params)
    return events, None


def durable(events: list[Event]) -> list[Event]:
    return [e for e in events if e.type != "llm.delta"]


def two_step() -> ScriptedProvider:
    return ScriptedProvider(
        [tool_response(("l", "list_dir", {}), text="Looking."), text_response("Done here.")]
    )


async def test_two_clients_watch_the_same_run_live(daemon_factory: Any) -> None:
    gate_holder: list[GatedProvider] = []

    def gated() -> ScriptedProvider:
        p = GatedProvider(two_step().script)
        gate_holder.append(p)
        return p

    d = await daemon_factory(gated)
    async with d.client("a") as a, d.client("b") as b:
        run_id = await start(a, d)
        watch_a = asyncio.create_task(collect(a, run_id))
        watch_b = asyncio.create_task(collect(b, run_id))
        await asyncio.sleep(0.1)  # both subscribed while the model is still "thinking"
        gate_holder[0].gate.set()
        (ev_a, end_a), (ev_b, end_b) = await asyncio.gather(watch_a, watch_b)

    assert end_a is not None and end_a.reason == "finished"
    assert [e.model_dump() for e in ev_a] == [e.model_dump() for e in ev_b]
    types = [e.type for e in durable(ev_a)]
    assert types[0] == "run.started" and types[-1] == "run.finished"
    assert any(e.type == "llm.delta" for e in ev_a)  # streamed text reached both
    seqs = [e.seq for e in durable(ev_a)]  # type: ignore[union-attr]
    assert seqs == list(range(len(seqs)))  # no gap, no duplicate


async def test_late_client_gets_a_replay_from_any_seq(daemon_factory: Any) -> None:
    d = await daemon_factory(two_step)
    async with d.client() as c:
        run_id = await start(c, d)
        live, _ = await collect(c, run_id)
    async with d.client() as late:
        full, end = await collect(late, run_id)
        tail, _ = await collect(late, run_id, from_seq=3)
    assert [e.seq for e in full] == [e.seq for e in durable(live)]  # type: ignore[union-attr]
    assert end is not None and end.reason == "finished"
    assert [e.seq for e in tail] == [e.seq for e in full][3:]  # type: ignore[union-attr]
    assert all(e.type != "llm.delta" for e in full)  # deltas are live-only


async def test_client_disconnect_does_not_stop_the_run(daemon_factory: Any) -> None:
    gates: list[GatedProvider] = []

    def gated() -> ScriptedProvider:
        gates.append(GatedProvider(two_step().script))
        return gates[-1]

    d = await daemon_factory(gated)
    a = d.client("a")
    await a.connect()
    run_id = await start(a, d)
    await a.call(RUN_SUBSCRIBE, RunSubscribeParams(run_id=run_id), RunSubscribeResult)
    await a.close()  # the client "crashes" mid-run
    gates[0].gate.set()
    async with d.client("b") as b:
        events, end = await collect(b, run_id)
    assert events[-1].type == "run.finished"
    assert events[-1].status == "completed"  # type: ignore[union-attr]


async def test_approval_round_trip_over_ipc(daemon_factory: Any) -> None:
    d = await daemon_factory(
        lambda: ScriptedProvider(
            [
                tool_response(("w1", "write_file", {"path": "out.txt", "content": "hi"})),
                text_response("wrote it"),
            ]
        )
    )
    async with d.client("a") as a, d.client("b") as b:
        run_id = await start(a, d, auto=False)
        await a.call(RUN_SUBSCRIBE, RunSubscribeParams(run_id=run_id), RunSubscribeResult)
        seen: list[Event] = []
        async for note in a.notifications():
            if note.method != EVENT_NOTIFICATION:
                break
            event = EVENT_ADAPTER.validate_python(note.params["event"])
            seen.append(event)
            if event.type == "tool.approval_requested":
                runs = await b.call(RUN_LIST, RunListParams(), RunListResult)
                assert runs.runs[0].pending_approvals == 1
                ok = await b.call(
                    APPROVAL_RESPOND,
                    ApprovalRespondParams(run_id=run_id, tool_use_id="w1", approve=True),
                    ApprovalRespondResult,
                )
                again = await a.call(
                    APPROVAL_RESPOND,
                    ApprovalRespondParams(run_id=run_id, tool_use_id="w1", approve=False),
                    ApprovalRespondResult,
                )
                assert ok.accepted and not again.accepted  # first answer wins
    resolved = next(e for e in seen if e.type == "tool.approval_resolved")
    assert (resolved.approved, resolved.by) == (True, "user")  # type: ignore[union-attr]
    assert (d.ws / "out.txt").read_text() == "hi"


async def test_unanswered_approval_times_out_as_denied(daemon_factory: Any) -> None:
    d = await daemon_factory(
        lambda: ScriptedProvider(
            [
                tool_response(("w1", "write_file", {"path": "x.txt", "content": "x"})),
                text_response("ok, not writing"),
            ]
        ),
        approval_timeout_s=0.2,
    )
    async with d.client() as c:
        run_id = await start(c, d, auto=False)
        events, _ = await collect(c, run_id)
    resolved = next(e for e in events if e.type == "tool.approval_resolved")
    assert (resolved.approved, resolved.by) == (False, "timeout")  # type: ignore[union-attr]
    assert not (d.ws / "x.txt").exists()


async def test_cancel_stops_a_live_run(daemon_factory: Any) -> None:
    d = await daemon_factory(lambda: GatedProvider(two_step().script))  # gate never opens
    async with d.client() as c:
        run_id = await start(c, d)
        watcher = asyncio.create_task(collect(c, run_id))
        await asyncio.sleep(0.05)
        first = await c.call(RUN_CANCEL, RunCancelParams(run_id=run_id), RunCancelResult)
        events, end = await watcher
        second = await c.call(RUN_CANCEL, RunCancelParams(run_id=run_id), RunCancelResult)
    assert first.cancelled and not second.cancelled
    assert events[-1].status == "cancelled"  # type: ignore[union-attr]


async def test_slow_client_is_cut_off_and_resumes_without_gaps(
    daemon_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_mod, "SUBSCRIBER_QUEUE_SIZE", 5)
    long_text = " ".join(f"w{i}" for i in range(200))  # ~200 deltas, fast
    gates: list[GatedProvider] = []

    def gated() -> ScriptedProvider:
        gates.append(GatedProvider([text_response(long_text)]))
        return gates[-1]

    d = await daemon_factory(gated)
    handle_sends: list[str] = []
    async with d.client() as c:
        run_id = await start(c, d)
        handle = d.app.runs.runs[run_id]
        release = asyncio.Event()

        async def slow_send(event: Event) -> None:
            handle_sends.append(event.type)
            await release.wait()  # this client reads nothing until released

        async def end_cb(info: StreamEnd) -> None:
            ends.append(info)

        ends: list[StreamEnd] = []
        sub = asyncio.create_task(d.app.runs.subscribe(run_id, 0, slow_send, end_cb))
        await asyncio.sleep(0.02)
        gates[0].gate.set()
        await handle.task  # the run finishes without waiting for the slow client
        release.set()
        await sub
        assert ends[0].reason == "lagged"
        resumed, end = await collect(c, run_id, from_seq=ends[0].next_seq)
    assert end is not None and end.reason == "finished"
    assert resumed[-1].type == "run.finished"
    assert resumed[0].seq == ends[0].next_seq  # type: ignore[union-attr]


async def test_finished_runs_replay_from_disk_after_restart(daemon_factory: Any) -> None:
    d1 = await daemon_factory(two_step)
    async with d1.client() as c:
        run_id = await start(c, d1)
        await collect(c, run_id)
    d2 = await daemon_factory(two_step)  # "restarted" daemon, same runs_dir, empty memory
    async with d2.client() as c:
        events, end = await collect(c, run_id)
    assert end is not None and end.reason == "replayed"
    assert events[-1].type == "run.finished"


async def test_shutdown_records_cancelled_runs(daemon_factory: Any, tmp_path: Path) -> None:
    d = await daemon_factory(lambda: GatedProvider(two_step().script))
    async with d.client() as c:
        run_id = await start(c, d)
        await asyncio.sleep(0.05)
    await d.app.runs.shutdown()
    lines = (tmp_path / "runs" / run_id / "events.jsonl").read_text().splitlines()
    assert json.loads(lines[-1])["status"] == "cancelled"


async def test_bad_requests_get_clear_errors(daemon_factory: Any) -> None:
    d = await daemon_factory(two_step)
    async with d.client() as c:
        with pytest.raises(RpcError) as exc:
            await c.call(RUN_SUBSCRIBE, RunSubscribeParams(run_id="nope"), RunSubscribeResult)
        assert exc.value.code == INVALID_PARAMS and "unknown run" in exc.value.message
        with pytest.raises(RpcError) as exc:
            await c.call(
                RUN_START, RunStartParams(goal="g", workspace="relative/dir"), RunStartResult
            )
        assert "workspace" in exc.value.message
