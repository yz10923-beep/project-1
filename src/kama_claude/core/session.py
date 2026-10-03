"""Sessions (S4): several runs in one conversation.

A session is a small JSON file listing its runs in order. It stores no messages: the
history is replayed from each run's events.jsonl (`agent.history.replay`), so there is
one source of truth, and a session can't disagree with the runs it is made of.

    <sessions_dir>/<session_id>.json   {"session_id", "workspace", "title", "runs": [...]}

Writes go to a temp file and are renamed into place, so a crash never leaves a
half-written session.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from kama_claude.core.agent.history import replay
from kama_claude.core.bus.events import EVENT_ADAPTER, Event
from kama_claude.core.llm.types import Message
from kama_claude.core.policy.engine import Rule


class SessionRun(BaseModel):
    run_id: str
    run_dir: str
    goal: str
    started_at: datetime
    status: str = "running"  # then the run's RunStatus
    finished_at: datetime | None = None


class SessionInfo(BaseModel):
    session_id: str
    workspace: str
    title: str
    created_at: datetime
    updated_at: datetime
    runs: list[SessionRun] = Field(default_factory=list)
    # S5: what the user answered "always allow" to, kept for the rest of the conversation.
    rules: list[Rule] = Field(default_factory=list)


class UnknownSession(Exception):
    pass


def new_session_id() -> str:
    return f"s{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def read_events(run_dir: Path) -> list[Event]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    return [EVENT_ADAPTER.validate_json(x) for x in path.read_text().splitlines() if x]


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    def _path(self, session_id: str) -> Path:
        if not session_id or "/" in session_id or session_id.startswith("."):
            raise UnknownSession(session_id)
        return self.root / f"{session_id}.json"

    def _write(self, info: SessionInfo) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-")
        with os.fdopen(fd, "w") as fh:
            fh.write(info.model_dump_json(indent=1))
        os.replace(tmp, self._path(info.session_id))

    def create(self, workspace: Path, title: str = "") -> SessionInfo:
        now = datetime.now(UTC)
        info = SessionInfo(
            session_id=new_session_id(),
            workspace=str(workspace.resolve()),
            title=title,
            created_at=now,
            updated_at=now,
        )
        self._write(info)
        return info

    def get(self, session_id: str) -> SessionInfo:
        path = self._path(session_id)
        if not path.is_file():
            raise UnknownSession(session_id)
        return SessionInfo.model_validate_json(path.read_text())

    def list_sessions(self, workspace: Path | None = None) -> list[SessionInfo]:
        """Newest activity first; only sessions in `workspace` if given."""
        if not self.root.is_dir():
            return []
        found = [SessionInfo.model_validate_json(p.read_text()) for p in self.root.glob("s*.json")]
        if workspace is not None:
            ws = str(workspace.resolve())
            found = [s for s in found if s.workspace == ws]
        return sorted(found, key=lambda s: s.updated_at, reverse=True)

    def add_run(self, session_id: str, run_id: str, run_dir: Path, goal: str) -> SessionInfo:
        info = self.get(session_id)
        now = datetime.now(UTC)
        info.runs.append(SessionRun(run_id=run_id, run_dir=str(run_dir), goal=goal, started_at=now))
        if not info.title:
            info.title = goal.strip().splitlines()[0][:80] if goal.strip() else run_id
        info.updated_at = now
        self._write(info)
        return info

    def finish_run(self, session_id: str, run_id: str, status: str) -> None:
        info = self.get(session_id)
        now = datetime.now(UTC)
        for r in info.runs:
            if r.run_id == run_id:
                r.status, r.finished_at = status, now
        info.updated_at = now
        self._write(info)

    def rules(self, session_id: str) -> list[Rule]:
        return self.get(session_id).rules

    def add_rules(self, session_id: str, rules: list[Rule]) -> None:
        info = self.get(session_id)
        known = [r.model_dump() for r in info.rules]
        info.rules += [r for r in rules if r.model_dump() not in known]
        self._write(info)

    def history(self, session_id: str, *, before_run: str | None = None) -> list[Message]:
        """The conversation so far: each run's events replayed in order (up to, not
        including, `before_run`). A run with no events (it never started) is skipped."""
        messages: list[Message] = []
        for r in self.get(session_id).runs:
            if r.run_id == before_run:
                break
            events = read_events(Path(r.run_dir))
            if events:
                messages = replay(messages, events)
        return messages
