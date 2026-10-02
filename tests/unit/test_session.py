"""S4 sessions: history rebuilt from run events, continued by the next run, repaired
when a run was interrupted; the store on disk."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kama_claude.core.agent.history import (
    INTERRUPTED_RESULT,
    conversation_problems,
    repair_orphans,
    replay,
)
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.bus.events import EVENT_ADAPTER, Event, RunStartedEvent
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMError, ToolCall
from kama_claude.core.session import SessionStore, UnknownSession, read_events
from kama_claude.core.tools.base import Tool, ToolContext, ToolResult
from tests.fakes import ScriptedProvider, text_response, tool_response


async def allow(_: ToolCall) -> bool:
    return True


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(runs_dir=tmp_path / "runs", sessions_dir=tmp_path / "sessions")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    (d / "venue.txt").write_text("ARCX\n")
    return d


def events_of(run_dir: Path) -> list[Event]:
    return read_events(run_dir)


async def run_in(
    settings: Settings, ws: Path, sid: str, goal: str, p: ScriptedProvider
) -> tuple[Any, Path]:
    return await run_goal(
        goal,
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p,
        session_id=sid,
        sessions=SessionStore(settings.sessions_dir),
    )


# ---------------------------------------------------------------- replay = what was sent


async def test_replay_rebuilds_exactly_what_the_model_was_sent(
    settings: Settings, ws: Path
) -> None:
    p = ScriptedProvider(
        [
            tool_response(("r", "read_file", {"path": "venue.txt"}), text="Reading."),
            tool_response(("l", "list_dir", {}), ("r2", "read_file", {"path": "nope"})),
            text_response("ARCX."),
        ]
    )
    _, run_dir = await run_goal(
        "which venue?", settings=settings, workspace=ws, approver=allow, provider=p
    )
    rebuilt = replay([], events_of(run_dir))
    assert rebuilt[:-1] == p.requests[-1].messages  # everything sent, in order
    assert rebuilt[-1]["role"] == "assistant"  # plus the final answer
    assert conversation_problems(rebuilt) == []


# ---------------------------------------------------------------- continuing a session


async def test_second_run_continues_the_first_runs_conversation(
    settings: Settings, ws: Path
) -> None:
    store = SessionStore(settings.sessions_dir)
    sid = store.create(ws).session_id
    p1 = ScriptedProvider(
        [tool_response(("r", "read_file", {"path": "venue.txt"})), text_response("ARCX.")]
    )
    _, dir1 = await run_in(settings, ws, sid, "which venue?", p1)
    p2 = ScriptedProvider([text_response("You asked about ARCX.")])
    _, dir2 = await run_in(settings, ws, sid, "what did I ask?", p2)

    sent = p2.requests[0].messages
    run1 = replay([], events_of(dir1))
    assert sent == [*run1, {"role": "user", "content": "what did I ask?"}]
    # the system prompt and earlier turns are byte-identical: the cache prefix holds
    assert p2.requests[0].system == p1.requests[0].system
    started = next(e for e in events_of(dir2) if isinstance(e, RunStartedEvent))
    assert (started.session_id, started.history_messages, started.repaired) == (sid, 4, 0)
    info = store.get(sid)
    assert [r.status for r in info.runs] == ["completed", "completed"]
    assert info.title == "which venue?"
    assert store.history(sid) == replay(run1, events_of(dir2))


async def test_memory_off_starts_every_run_fresh(settings: Settings, ws: Path) -> None:
    off = settings.model_copy(update={"memory": False})
    store = SessionStore(off.sessions_dir)
    sid = store.create(ws).session_id
    await run_in(off, ws, sid, "one", ScriptedProvider([text_response("1")]))
    p2 = ScriptedProvider([text_response("2")])
    await run_in(off, ws, sid, "two", p2)
    assert p2.requests[0].messages == [{"role": "user", "content": "two"}]
    assert len(store.get(sid).runs) == 2  # still recorded in the session


# ---------------------------------------------------------------- repairs


class BlockParams(BaseModel):
    pass


class Block(Tool[BlockParams]):
    """A tool that never finishes on its own: the run gets cancelled while it waits."""

    name = "block"
    description = "waits forever"
    params_model = BlockParams
    started = asyncio.Event()

    async def run(self, params: BlockParams, ctx: ToolContext) -> ToolResult:
        Block.started.set()
        await asyncio.Event().wait()
        return ToolResult("unreachable")


async def test_run_cancelled_mid_tool_is_repaired_for_the_next_run(
    settings: Settings, ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kama_claude.core.agent import runner

    real = runner.builtin_tools
    monkeypatch.setattr(runner, "builtin_tools", lambda: [*real(), Block()])
    Block.started = asyncio.Event()
    store = SessionStore(settings.sessions_dir)
    sid = store.create(ws).session_id
    # one turn, two calls: list_dir finishes, then the run is cancelled inside `block`
    p1 = ScriptedProvider([tool_response(("l", "list_dir", {}), ("b", "block", {}))])
    task = asyncio.create_task(run_in(settings, ws, sid, "go", p1))
    await Block.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get(sid).runs[0].status == "cancelled"

    # The stored history is what happened: list_dir answered, block not. The API would
    # reject it as is; the next run repairs it on the way in and records that it did.
    history = store.history(sid)
    assert conversation_problems(history) == ["tool_use ['l', 'b'] at 1 answered by ['l']"]
    repaired, n = repair_orphans(history)
    assert n == 1 and conversation_problems(repaired) == []
    results = repaired[-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["l", "b"]  # in tool_use order
    assert results[1] == {
        "type": "tool_result",
        "tool_use_id": "b",
        "content": INTERRUPTED_RESULT,
        "is_error": True,
    }
    p2 = ScriptedProvider([text_response("ok")])
    _, dir2 = await run_in(settings, ws, sid, "try again", p2)
    sent = p2.requests[0].messages
    assert conversation_problems(sent) == []
    assert sent[-1]["content"][-1] == {"type": "text", "text": "try again"}  # joined
    started = next(e for e in events_of(dir2) if isinstance(e, RunStartedEvent))
    assert started.repaired == 1  # the run that repaired it says so
    assert conversation_problems(store.history(sid)) == []  # and replay repeats the repair


def test_repair_answers_a_bare_tool_use_turn() -> None:
    history = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}],
        },
    ]
    repaired, n = repair_orphans(history)
    assert n == 1 and conversation_problems(repaired) == []
    assert history[-1]["role"] == "assistant"  # the input is not modified


async def test_run_ending_on_a_user_turn_joins_the_next_goal(settings: Settings, ws: Path) -> None:
    store = SessionStore(settings.sessions_dir)
    sid = store.create(ws).session_id
    # API error on the very first call: history is just the goal
    await run_in(settings, ws, sid, "first", ScriptedProvider([LLMError("boom", retryable=True)]))
    # max_steps right after tools ran: history ends with the tool results
    one_step = settings.model_copy(update={"max_steps": 1})
    await run_in(
        one_step, ws, sid, "second", ScriptedProvider([tool_response(("l", "list_dir", {}))])
    )
    p3 = ScriptedProvider([text_response("ok")])
    await run_in(settings, ws, sid, "third", p3)
    sent = p3.requests[0].messages
    assert conversation_problems(sent) == []
    assert sent[0]["content"] == [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    assert sent[-1]["content"][0]["type"] == "tool_result"
    assert sent[-1]["content"][-1] == {"type": "text", "text": "third"}


def test_validator_catches_what_the_api_would_reject() -> None:
    use = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}],
    }
    assert conversation_problems([{"role": "assistant", "content": "hi"}])
    assert conversation_problems(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    )
    assert conversation_problems([{"role": "user", "content": "a"}, use])  # unanswered
    late = {
        "role": "user",
        "content": [
            {"type": "text", "text": "x"},
            {"type": "tool_result", "tool_use_id": "t", "content": "r"},
        ],
    }
    assert conversation_problems([{"role": "user", "content": "a"}, use, late])


# ---------------------------------------------------------------- the store


def test_store_roundtrip_listing_and_bad_ids(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    a = store.create(tmp_path, "first")
    b = store.create(tmp_path / "other" if (tmp_path / "other").mkdir() is None else tmp_path)
    store.add_run(a.session_id, "r1", tmp_path / "r1", "goal")
    store.finish_run(a.session_id, "r1", "completed")
    got = store.get(a.session_id)
    assert got.title == "first" and [(r.run_id, r.status) for r in got.runs] == [
        ("r1", "completed")
    ]
    assert [s.session_id for s in store.list_sessions()][0] == a.session_id  # newest activity
    assert [s.session_id for s in store.list_sessions(tmp_path)] == [a.session_id]
    assert b.session_id in {s.session_id for s in store.list_sessions()}
    assert not list((tmp_path / "sessions").glob(".tmp-*"))  # atomic writes left nothing
    for bad in ("nope", "../x", ""):
        with pytest.raises(UnknownSession):
            store.get(bad)
    assert store.history(a.session_id) == []  # a run without events is skipped


def test_events_still_parse_without_session_fields() -> None:
    old = (
        '{"type":"run.started","run_id":"r","seq":0,"at":"2026-09-01T00:00:00Z",'
        '"goal":"g","model":"m","workspace":"/w","max_steps":30}'
    )
    e = EVENT_ADAPTER.validate_json(old)
    assert isinstance(e, RunStartedEvent) and e.session_id is None and e.preamble is None
