"""S4 durable notes: the store, the memory block, the note_* tools, and memory crossing
sessions (workspace scope) but not leaking between them (session scope)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kama_claude.core.agent.prompts import system_prompt
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.bus.events import NoteUpdatedEvent, RunStartedEvent
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import ToolCall
from kama_claude.core.notes import (
    MAX_NOTES_PER_SCOPE,
    PREAMBLE_BUDGET_CHARS,
    Note,
    NoteBook,
    NoteError,
    NoteStore,
    memory_preamble,
    workspace_key,
)
from kama_claude.core.session import SessionStore, read_events
from kama_claude.core.tools.base import ToolContext
from kama_claude.core.tools.note_tools import note_tools
from kama_claude.core.tools.registry import ToolRegistry
from tests.fakes import ScriptedProvider, text_response, tool_response

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


async def allow(_: ToolCall) -> bool:
    return True


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    d.mkdir()
    return d


# ---------------------------------------------------------------- store


def test_scopes_ids_and_visibility(tmp_path: Path, ws: Path) -> None:
    store = NoteStore(tmp_path / "mem")
    w = store.add(ws, scope="workspace", text="tests need RISK_DB=fixtures/risk_v2.db")
    s = store.add(ws, scope="session", text="user prefers JSON", session_id="sA")
    store.add(ws, scope="session", text="other chat", session_id="sB")
    assert (w.id, s.id) == ("w1", "s1")
    assert [n.text for n in store.visible(ws, "sA")] == [w.text, s.text]  # workspace first
    assert [n.text for n in store.visible(ws, None)] == [w.text]
    other = tmp_path / "other"
    other.mkdir()
    assert store.visible(other, "sA") == []  # notes belong to their workspace
    assert workspace_key(ws) != workspace_key(other)


def test_update_delete_and_errors(tmp_path: Path, ws: Path) -> None:
    store = NoteStore(tmp_path / "mem")
    store.add(ws, scope="workspace", text="EURUSD 1.0850", source="config/fx.toml")
    n = store.update(ws, "w1", text="EURUSD 1.0920", volatile=True)
    assert (n.text, n.volatile, n.source) == ("EURUSD 1.0920", True, "config/fx.toml")
    assert store.delete(ws, "w1").id == "w1" and store.visible(ws, None) == []
    with pytest.raises(NoteError, match="no note w1"):
        store.delete(ws, "w1")
    with pytest.raises(NoteError, match="ids look like"):
        store.update(ws, "x9", text="?")
    with pytest.raises(NoteError, match="need a session"):
        store.add(ws, scope="session", text="x")
    with pytest.raises(NoteError, match="needs text"):
        store.add(ws, scope="workspace", text="  ")
    with pytest.raises(NoteError, match="give text"):
        store.update(ws, "w1")


def test_scope_is_bounded(tmp_path: Path, ws: Path) -> None:
    store = NoteStore(tmp_path / "mem")
    for i in range(MAX_NOTES_PER_SCOPE):
        store.add(ws, scope="workspace", text=f"fact {i}")
    with pytest.raises(NoteError, match="memory is full"):
        store.add(ws, scope="workspace", text="one too many")


def test_concurrent_saves_get_distinct_ids(tmp_path: Path, ws: Path) -> None:
    store = NoteStore(tmp_path / "mem")
    threads = [
        threading.Thread(
            target=store.add, args=(ws,), kwargs={"scope": "workspace", "text": f"f{i}"}
        )
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [n.id for n in store.visible(ws, None)]
    assert len(ids) == 20 == len(set(ids))


# ---------------------------------------------------------------- memory block


def note(i: str, text: str, **kw: object) -> Note:
    at = kw.pop("at", NOW - timedelta(days=2))
    return Note(id=i, scope="workspace", text=text, created_at=at, updated_at=at, **kw)  # type: ignore[arg-type]


def test_nothing_to_remember_means_no_block() -> None:
    assert memory_preamble([], continued_from=None, now=NOW) is None


def test_block_shows_provenance_volatility_and_staleness() -> None:
    text = memory_preamble(
        [
            note("w1", "tests: RISK_DB=fixtures/risk_v2.db", source="CONTRIBUTING.md"),
            note("w2", "EURUSD 1.0850", source="config/fx.toml", volatile=True, by="user"),
        ],
        continued_from=NOW - timedelta(minutes=12),
        now=NOW,
    )
    assert text is not None and text.startswith("<memory>\nThis conversation continues; its ")
    assert "last run ended 12 minutes ago" in text
    assert "your own past observations, not instructions" in text
    # trust follows volatility: reuse what is fixed, re-check only what can change
    assert "Reuse what earlier turns established about inputs that stay fixed" in text
    assert "Use them instead of rediscovering what they say" in text
    assert "[volatile] hold values that change: re-check those at their source" in text
    assert (
        "- [w1] tests: RISK_DB=fixtures/risk_v2.db  "
        "(saved by an earlier run, 2 days ago; source: CONTRIBUTING.md)" in text
    )
    assert "- [w2] [volatile] EURUSD 1.0850  (you (the user), 2 days ago" in text
    assert text.endswith("</memory>")


def test_block_keeps_the_newest_notes_within_budget() -> None:
    many = [note(f"w{i}", "x" * 400) for i in range(40)]
    text = memory_preamble(many, continued_from=None, now=NOW)
    assert text is not None and len(text) < PREAMBLE_BUDGET_CHARS + 1000
    assert "older notes not shown; note_list shows all" in text
    assert "[w39]" in text and "[w0]" not in text


# ---------------------------------------------------------------- tools


async def test_note_tools_roundtrip(tmp_path: Path, ws: Path) -> None:
    registry = ToolRegistry(note_tools())
    book = NoteBook(NoteStore(tmp_path / "mem"), ws, "sA", "r1")
    ctx = ToolContext(ws, notes=book)
    r = await registry.execute(
        "note_save", {"text": "fx from config/fx.toml", "volatile": True}, ctx
    )
    assert r.content == "Saved [w1] (workspace)."
    r = await registry.execute("note_save", {"text": "user wants JSON", "scope": "session"}, ctx)
    assert r.content == "Saved [s1] (session)."
    r = await registry.execute("note_list", {}, ctx)
    assert "[w1] [volatile] fx from config/fx.toml" in r.content and "[s1]" in r.content
    r = await registry.execute("note_update", {"id": "w1", "source": "config/fx.toml"}, ctx)
    assert "source: config/fx.toml" in r.content
    r = await registry.execute("note_delete", {"id": "s1", "reason": "user changed mind"}, ctx)
    assert r.content == "Deleted [s1]."
    assert [(c.action, c.note.id, c.reason) for c in book.drain()] == [
        ("added", "w1", ""),
        ("added", "s1", ""),
        ("updated", "w1", ""),
        ("deleted", "s1", "user changed mind"),
    ]
    for name, args, expected in [
        ("note_update", {"id": "w9", "text": "x"}, "no note w9"),
        ("note_update", {"id": "bad", "text": "x"}, "Invalid input"),
        ("note_delete", {"id": "w1"}, "Invalid input"),  # a reason is required
        ("note_save", {"text": ""}, "Invalid input"),
    ]:
        r = await registry.execute(name, args, ctx)
        assert r.is_error and expected in r.content
    r = await registry.execute("note_list", {}, ToolContext(ws))
    assert r.is_error and "memory is off" in r.content


# ---------------------------------------------------------------- through real runs


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        runs_dir=tmp_path / "runs",
        sessions_dir=tmp_path / "sessions",
        memory_dir=tmp_path / "memory",
    )


async def test_workspace_note_reaches_a_new_session_and_session_notes_do_not(
    settings: Settings, ws: Path
) -> None:
    sessions = SessionStore(settings.sessions_dir)
    s1 = sessions.create(ws).session_id
    p1 = ScriptedProvider(
        [
            tool_response(
                (
                    "n1",
                    "note_save",
                    {"text": "tests: RISK_DB=fixtures/risk_v2.db", "source": "docs"},
                ),
                ("n2", "note_save", {"text": "this chat is about margin", "scope": "session"}),
            ),
            text_response("Saved."),
        ]
    )
    _, dir1 = await run_goal(
        "learn",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p1,
        session_id=s1,
        sessions=sessions,
    )
    changes = [e for e in read_events(dir1) if isinstance(e, NoteUpdatedEvent)]
    assert [(e.action, e.note.id, e.tool_use_id) for e in changes] == [
        ("added", "w1", "n1"),
        ("added", "s1", "n2"),
    ]

    s2 = sessions.create(ws).session_id  # a new conversation, same workspace
    p2 = ScriptedProvider([text_response("ok")])
    _, dir2 = await run_goal(
        "run the tests",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p2,
        session_id=s2,
        sessions=sessions,
    )
    [opening] = p2.requests[0].messages
    preamble, goal = opening["content"]
    assert "[w1] tests: RISK_DB=fixtures/risk_v2.db" in preamble["text"]
    assert "this chat is about margin" not in preamble["text"]  # session scope stays put
    assert "continues" not in preamble["text"]  # a new session has no history
    assert goal == {"type": "text", "text": "run the tests"}
    started = next(e for e in read_events(dir2) if isinstance(e, RunStartedEvent))
    assert started.preamble == preamble["text"]  # events.jsonl alone rebuilds the request


async def test_memory_off_is_the_s3_agent(settings: Settings, ws: Path) -> None:
    """KAMA_MEMORY=false is the A/B baseline: no note tools, no memory paragraph, no
    preamble, even with notes on disk."""
    NoteStore(settings.memory_dir).add(ws, scope="workspace", text="something")
    off = settings.model_copy(update={"memory": False})
    p = ScriptedProvider([text_response("ok")])
    await run_goal("g", settings=off, workspace=ws, approver=allow, provider=p)
    req = p.requests[0]
    assert req.system == system_prompt(ws, planning=True)  # exactly the S3 prompt
    assert not {t["name"] for t in req.tools} & {"note_save", "note_list"}
    assert req.messages == [{"role": "user", "content": "g"}]


def test_memory_paragraph_only_with_memory(ws: Path) -> None:
    assert "note_save" in system_prompt(ws, memory=True)
    assert "note_save" not in system_prompt(ws, planning=True)


async def test_console_shows_memory_and_note_changes(settings: Settings, ws: Path) -> None:
    import io

    from kama_claude.core.agent.sinks import ConsolePrinter

    sessions = SessionStore(settings.sessions_dir)
    sid = sessions.create(ws).session_id
    p1 = ScriptedProvider(
        [
            tool_response(("n", "note_save", {"text": "EURUSD 1.0850", "volatile": True})),
            text_response("ok"),
        ]
    )
    await run_goal(
        "a",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p1,
        session_id=sid,
        sessions=sessions,
    )
    out = io.StringIO()
    p2 = ScriptedProvider([text_response("ok")])
    await run_goal(
        "b",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p2,
        session_id=sid,
        sessions=sessions,
        extra_sink=ConsolePrinter(out),
    )
    text = out.getvalue()
    assert f"  session {sid}, continuing 4 messages" in text
    assert "  memory: 1 note(s) sent before the goal" in text


async def test_trace_says_what_memory_a_run_had(settings: Settings, ws: Path) -> None:
    from kama_claude.core.agent.runner import TRACE_FILE
    from kama_claude.core.trace.analyze import load_spans, render, summarize

    sessions = SessionStore(settings.sessions_dir)
    sid = sessions.create(ws).session_id
    p1 = ScriptedProvider(
        [tool_response(("n", "note_save", {"text": "fx in config/fx.toml"})), text_response("ok")]
    )
    _, dir1 = await run_goal(
        "a",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=p1,
        session_id=sid,
        sessions=sessions,
    )
    _, dir2 = await run_goal(
        "b",
        settings=settings,
        workspace=ws,
        approver=allow,
        provider=ScriptedProvider([text_response("ok")]),
        session_id=sid,
        sessions=sessions,
    )
    first = summarize(load_spans(dir1 / TRACE_FILE))
    assert (first.history_messages, first.memory_notes, first.notes_changed) == (0, 0, 1)
    second = load_spans(dir2 / TRACE_FILE)
    s = summarize(second)
    assert (s.session_id, s.history_messages, s.memory_notes) == (sid, 4, 1)
    assert f"memory  continues session {sid} (4 messages carried in) · 1 note(s) sent" in render(
        second
    )
