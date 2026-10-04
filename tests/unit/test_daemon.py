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
    NOTES_ADD,
    NOTES_DELETE,
    NOTES_LIST,
    NOTES_UPDATE,
    PLAN_EDIT,
    PLAN_GET,
    RUN_CANCEL,
    RUN_LIST,
    RUN_START,
    RUN_SUBSCRIBE,
    SESSION_CREATE,
    SESSION_GET,
    SESSION_LIST,
    STREAM_END_NOTIFICATION,
    ApprovalRespondParams,
    ApprovalRespondResult,
    NoteResult,
    NotesAddParams,
    NotesDeleteParams,
    NotesListParams,
    NotesListResult,
    NotesUpdateParams,
    PlanEditParams,
    PlanEditResult,
    PlanGetParams,
    PlanGetResult,
    RunCancelParams,
    RunCancelResult,
    RunListParams,
    RunListResult,
    RunStartParams,
    RunStartResult,
    RunSubscribeParams,
    RunSubscribeResult,
    SessionCreateParams,
    SessionGetParams,
    SessionListParams,
    SessionListResult,
    SessionView,
    StreamEnd,
)
from kama_claude.core.bus.envelope import INVALID_PARAMS
from kama_claude.core.bus.events import EVENT_ADAPTER, Event, ToolFinishedEvent
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMProvider
from kama_claude.core.plan import NewTask, TaskChange
from kama_claude.core.transport.client import JsonRpcClient, RpcError
from tests.fakes import (
    GatedProvider,
    PausingProvider,
    ScriptedProvider,
    text_response,
    tool_response,
)


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


async def test_trace_covers_ipc_and_event_bus_layers(daemon_factory: Any, tmp_path: Path) -> None:
    from kama_claude.core.agent.runner import TRACE_FILE
    from kama_claude.core.trace.analyze import load_spans, summarize

    gates: list[GatedProvider] = []

    def gated() -> ScriptedProvider:
        gates.append(GatedProvider(two_step().script))
        return gates[-1]

    d = await daemon_factory(gated)
    async with d.client("alice") as a, d.client("bob") as b:
        run_id = await start(a, d)
        watchers = [asyncio.create_task(collect(c, run_id)) for c in (a, b)]
        await asyncio.sleep(0.05)
        gates[0].gate.set()
        await asyncio.gather(*watchers)
    await asyncio.sleep(0.05)  # subscription spans are written as each stream ends

    spans = load_spans(tmp_path / "runs" / run_id / TRACE_FILE)
    names = [s.name for s in spans]
    assert "rpc run.start" in names  # the run id came from the result, not the params
    assert names.count("rpc run.subscribe") == 2
    subs = [s for s in spans if s.name == "bus.subscribe"]
    assert {s.attrs["client"] for s in subs} == {"alice", "bob"}
    assert all(s.attrs["ended"] == "finished" and s.attrs["live_events"] > 0 for s in subs)
    assert all(s.attrs["lag_max_ms"] >= s.attrs["lag_mean_ms"] >= 0 for s in subs)
    assert summarize(spans).ipc["run.subscribe"]["count"] == 2

    daemon_trace = (tmp_path / "runs" / "_daemon" / TRACE_FILE).read_text()
    assert "rpc core.hello" in daemon_trace
    assert d.app.token not in daemon_trace  # the auth token is never logged


def test_runs_share_one_sdk_client_per_api_key(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from kama_claude.core.agent.manager import RunManager

    manager = RunManager(Settings(runs_dir=tmp_path, anthropic_api_key="k1"))  # type: ignore[arg-type]
    a = manager._shared_client_provider(manager._settings)
    b = manager._shared_client_provider(manager._settings.model_copy(update={"model": "x"}))
    other = manager._shared_client_provider(
        manager._settings.model_copy(update={"anthropic_api_key": SecretStr("k2")})
    )
    assert a._client is b._client  # type: ignore[attr-defined]
    assert a._client is not other._client  # type: ignore[attr-defined]


async def test_plan_progress_is_visible_to_late_clients_and_run_list(
    daemon_factory: Any,
) -> None:
    """S3: the plan lives in the event stream, so a client that attaches mid-run
    rebuilds it from the replay, and `kama runs` shows progress without subscribing."""

    def planned() -> ScriptedProvider:
        return ScriptedProvider(
            [
                tool_response(
                    ("c", "task_create", {"tasks": [{"title": "inspect"}, {"title": "run it"}]}),
                    ("u1", "task_update", {"updates": [{"id": 1, "status": "in_progress"}]}),
                ),
                tool_response(
                    ("u2", "task_update", {"updates": [{"id": 1, "status": "completed"}]}),
                    ("b", "bash", {"command": "echo hi > hi.txt"}),  # waits for approval
                ),
                tool_response(
                    ("u3", "task_update", {"updates": [{"id": 2, "status": "completed"}]})
                ),
                text_response("done"),
            ]
        )

    d = await daemon_factory(planned)
    async with d.client("starter") as c:
        run_id = await start(c, d, auto=False)
        for _ in range(500):  # wait for the run to block on the bash approval
            [info] = (await c.call(RUN_LIST, RunListParams(), RunListResult)).runs
            if info.pending_approvals:
                break
            await asyncio.sleep(0.01)
        assert (info.plan_done, info.plan_total) == (1, 2)

        async with d.client("late") as late:
            watching = asyncio.create_task(collect(late, run_id))
            # No wait needed: replay + live has no gap, whenever the subscribe lands.
            await c.call(
                APPROVAL_RESPOND,
                ApprovalRespondParams(run_id=run_id, tool_use_id="b", approve=True),
                ApprovalRespondResult,
            )
            events, end = await watching
        [info] = (await c.call(RUN_LIST, RunListParams(), RunListResult)).runs

    assert end is not None and end.reason == "finished"
    snaps = [e for e in events if e.type == "plan.updated"]
    assert [s.tool_use_id for s in snaps] == ["c", "u1", "u2", "u3"]  # type: ignore[union-attr]
    assert [t.status for t in snaps[-1].tasks] == ["completed", "completed"]  # type: ignore[union-attr]
    assert (info.plan_done, info.plan_total) == (2, 2)


async def test_user_steers_a_live_plan_over_ipc(daemon_factory: Any) -> None:
    """S3 steering: a client edits a live run's plan; the model is told at its next
    call; the plan is readable live, and from disk after a restart."""

    def planned() -> ScriptedProvider:
        return PausingProvider(
            [
                tool_response(("c", "task_create", {"tasks": [{"title": "a"}, {"title": "b"}]})),
                tool_response(
                    (
                        "u",
                        "task_update",
                        {
                            "updates": [
                                {"id": 1, "status": "completed"},
                                {"id": 3, "status": "completed"},
                            ]
                        },
                    )
                ),
                text_response("done"),
            ],
            pause_at={1},
        )

    d = await daemon_factory(planned)
    async with d.client("tui") as c:
        run_id = await start(c, d)
        provider = d.providers[0]
        assert isinstance(provider, PausingProvider)
        await provider.paused.wait()
        edit = await c.call(
            PLAN_EDIT,
            PlanEditParams(
                run_id=run_id,
                add=[NewTask(title="write the summary")],
                changes=[TaskChange(id=2, status="cancelled", note="not needed")],
            ),
            PlanEditResult,
        )
        assert (
            edit.summary == "task 2: pending -> cancelled (not needed); added 3. write the summary"
        )
        got = await c.call(PLAN_GET, PlanGetParams(run_id=run_id), PlanGetResult)
        assert got.live and [t.added_by for t in got.tasks] == ["model", "model", "user"]
        with pytest.raises(RpcError) as exc:  # a bad edit is a clear -32602, plan unchanged
            await c.call(
                PLAN_EDIT,
                PlanEditParams(run_id=run_id, changes=[TaskChange(id=9, note="x")]),
                PlanEditResult,
            )
        assert exc.value.code == INVALID_PARAMS and "no task 9" in exc.value.message
        provider.resume.set()
        events, _ = await collect(c, run_id)

        assert [e.type for e in events].count("plan.notice") == 1
        assert (
            provider.requests[2]
            .messages[-1]["content"][-1]["text"]
            .startswith("[The user changed your plan")
        )
        with pytest.raises(RpcError) as exc:
            await c.call(
                PLAN_EDIT,
                PlanEditParams(run_id=run_id, add=[NewTask(title="late")]),
                PlanEditResult,
            )
        assert "not running" in exc.value.message

    d2 = await daemon_factory(planned)  # restarted: the plan comes back from events.jsonl
    async with d2.client() as c:
        got = await c.call(PLAN_GET, PlanGetParams(run_id=run_id), PlanGetResult)
        assert not got.live
        assert [(t.id, t.status) for t in got.tasks] == [
            (1, "completed"),
            (2, "cancelled"),
            (3, "completed"),
        ]
        with pytest.raises(RpcError) as exc:
            await c.call(PLAN_GET, PlanGetParams(run_id="nope"), PlanGetResult)
        assert "unknown run" in exc.value.message


# ---------------------------------------------------------------- S4: sessions and notes


async def finished(c: JsonRpcClient, run_id: str) -> None:
    _, end = await collect(c, run_id)
    assert end is not None and end.reason == "finished"


async def test_session_continues_over_ipc_one_run_at_a_time(daemon_factory: Any) -> None:
    def script() -> ScriptedProvider:
        return PausingProvider([text_response("answer")], pause_at={0})

    d = await daemon_factory(script)
    async with d.client("cli") as c:
        first = await c.call(
            RUN_START,
            RunStartParams(goal="one", workspace=str(d.ws), auto_approve=True, new_session=True),
            RunStartResult,
        )
        sid = first.session_id
        assert sid is not None
        await until_paused(d, 0)
        # a second run in the same session while the first is live: refused, clearly
        with pytest.raises(RpcError) as exc:
            await c.call(
                RUN_START,
                RunStartParams(goal="two", workspace=str(d.ws), session_id=sid),
                RunStartResult,
            )
        assert exc.value.code == INVALID_PARAMS
        assert f"already has a run in progress: {first.run_id}" in exc.value.message
        view = await c.call(SESSION_GET, SessionGetParams(session_id=sid), SessionView)
        assert view.active_run_id == first.run_id
        d.providers[0].resume.set()  # type: ignore[attr-defined]
        await finished(c, first.run_id)

        second = await c.call(
            RUN_START,
            RunStartParams(goal="two", workspace=str(d.ws), auto_approve=True, session_id=sid),
            RunStartResult,
        )
        d.providers[1].resume.set()  # type: ignore[attr-defined]
        await finished(c, second.run_id)
        sent = d.providers[1].requests[0].messages
        assert [m["role"] for m in sent] == ["user", "assistant", "user"]  # run 1, then run 2
        assert sent[1]["content"][0]["text"] == "answer"
        [info] = [
            r
            for r in (await c.call(RUN_LIST, RunListParams(), RunListResult)).runs
            if r.run_id == second.run_id
        ]
        assert info.session_id == sid
        view = await c.call(SESSION_GET, SessionGetParams(session_id=sid), SessionView)
        assert [r.run_id for r in view.runs] == [first.run_id, second.run_id]
        assert view.active_run_id is None
        listed = await c.call(
            SESSION_LIST, SessionListParams(workspace=str(d.ws)), SessionListResult
        )
        assert [s.session_id for s in listed.sessions] == [sid]

    d2 = await daemon_factory(script)  # restarted daemon: the session is on disk
    async with d2.client() as c:
        third = await c.call(
            RUN_START,
            RunStartParams(goal="three", workspace=str(d2.ws), auto_approve=True, session_id=sid),
            RunStartResult,
        )
        d2.providers[0].resume.set()  # type: ignore[attr-defined]
        await finished(c, third.run_id)
        assert len(d2.providers[0].requests[0].messages) == 5  # runs 1 and 2, then 3


async def until_paused(d: Daemon, index: int) -> None:
    for _ in range(500):
        if len(d.providers) > index and d.providers[index].paused.is_set():  # type: ignore[attr-defined]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("provider never paused")


async def test_session_errors_are_clear(daemon_factory: Any, tmp_path: Path) -> None:
    d = await daemon_factory(two_step)
    other = tmp_path / "other"
    other.mkdir()
    async with d.client() as c:
        sid = (
            await c.call(SESSION_CREATE, SessionCreateParams(workspace=str(d.ws)), SessionView)
        ).session_id
        for params, expected in [
            (RunStartParams(goal="g", workspace=str(other), session_id=sid), "works in"),
            (RunStartParams(goal="g", workspace=str(d.ws), session_id="s-nope"), "unknown session"),
        ]:
            with pytest.raises(RpcError) as exc:
                await c.call(RUN_START, params, RunStartResult)
            assert exc.value.code == INVALID_PARAMS and expected in exc.value.message
        with pytest.raises(RpcError) as exc:
            await c.call(SESSION_GET, SessionGetParams(session_id="s-nope"), SessionView)
        assert "unknown session" in exc.value.message


async def test_user_notes_over_ipc_reach_the_next_run(daemon_factory: Any) -> None:
    d = await daemon_factory(lambda: ScriptedProvider([text_response("ok")]))
    ws = str(d.ws)
    async with d.client("cli") as c:
        added = await c.call(
            NOTES_ADD,
            NotesAddParams(workspace=ws, text="Reports go to the risk desk by 17:00", source="me"),
            NoteResult,
        )
        assert (added.note.id, added.note.by) == ("w1", "user")
        await c.call(
            NOTES_UPDATE, NotesUpdateParams(workspace=ws, note_id="w1", volatile=True), NoteResult
        )
        run_id = await start(c, d)
        await finished(c, run_id)
        preamble = d.providers[0].requests[0].messages[0]["content"][0]["text"]
        assert "[w1] [volatile] Reports go to the risk desk by 17:00  (you (the user)" in preamble
        await c.call(NOTES_DELETE, NotesDeleteParams(workspace=ws, note_id="w1"), NoteResult)
        listed = await c.call(NOTES_LIST, NotesListParams(workspace=ws), NotesListResult)
        assert listed.notes == []
        with pytest.raises(RpcError) as exc:
            await c.call(NOTES_DELETE, NotesDeleteParams(workspace=ws, note_id="w7"), NoteResult)
        assert exc.value.code == INVALID_PARAMS and "no note w7" in exc.value.message


async def test_notes_commands_say_when_memory_is_off(daemon_factory: Any) -> None:
    d = await daemon_factory(two_step, memory=False)
    async with d.client() as c:
        with pytest.raises(RpcError) as exc:
            await c.call(NOTES_LIST, NotesListParams(workspace=str(d.ws)), NotesListResult)
        assert "memory is off" in exc.value.message


async def test_a_broken_policy_file_rejects_the_run_and_frees_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kama_claude.core.agent.manager import RunManager
    from kama_claude.core.policy.engine import PolicyFileError

    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "policy.toml").write_text('[[rules]]\naction = "sometimes"\n')
    mgr = RunManager(
        Settings(
            runs_dir=tmp_path / "runs",
            sessions_dir=tmp_path / "sessions",
            policy_file=tmp_path / "policy.toml",
            sandbox="off",
        ),
        provider_factory=lambda s: ScriptedProvider([text_response("x")]),
    )
    with pytest.raises(PolicyFileError):
        await mgr.start("go", ws, auto_approve=True, new_session=True)
    [info] = mgr.sessions.list_sessions()
    assert [r.status for r in info.runs] == ["error"]
    assert mgr.active_run(info.session_id) is None


async def test_daemon_runs_keep_cut_output_readable(daemon_factory: Any, tmp_path: Path) -> None:
    """S6: the daemon's runs get the output store too (the manager builds its own loop)."""

    def script() -> ScriptedProvider:
        return ScriptedProvider(
            [
                tool_response(("t1", "bash", {"command": "seq 1 40000"})),
                tool_response(("t2", "read_output", {"id": "t1", "offset": 3, "limit": 1})),
                text_response("read it"),
            ]
        )

    d = await daemon_factory(script)
    async with d.client() as c:
        run_id = await start(c, d)
        events, _ = await collect(c, run_id)
    done = {e.tool_use_id: e for e in events if isinstance(e, ToolFinishedEvent)}
    assert done["t1"].cut is not None and done["t2"].output.splitlines()[0] == "     3\t2"
    assert any((tmp_path / "runs").rglob("outputs/t1.txt"))
