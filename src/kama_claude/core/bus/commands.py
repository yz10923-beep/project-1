"""Command contract: one params model and one result model per JSON-RPC method.

Adding a command = add both models here, register a handler in CoreApp,
and add a client call. The method name is the only routing key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.notes import Note, NoteScope
from kama_claude.core.plan import NewTask, PlanTask, TaskChange
from kama_claude.core.session import SessionInfo


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- connection

HELLO = "core.hello"  # must be the first request on a connection when auth is on


class HelloParams(_Params):
    token: str
    client: str = Field(min_length=1)


class HelloResult(BaseModel):
    ok: bool = True


PING = "core.ping"


class PingParams(_Params):
    client: str = Field(min_length=1, description="Name of the calling client, e.g. 'kama-cli'.")


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int = Field(ge=0)
    received_at: datetime
    # S5: what protects tool calls on this daemon.
    policy: bool | None = None
    sandbox: str | None = Field(default=None, description="bwrap | unshare | none, and why.")


# ---- runs

RUN_START = "run.start"


class RunStartParams(_Params):
    goal: str = Field(min_length=1)
    workspace: str = Field(description="Absolute path of the directory the agent works in.")
    model: str | None = None
    max_steps: int | None = Field(default=None, ge=1)
    auto_approve: bool = Field(default=False, description="Approve bash/write_file without asking.")
    # S4: continue a session (its history and notes), or start a new one for this run.
    session_id: str | None = Field(default=None, description="Continue this session.")
    new_session: bool = Field(default=False, description="Start a session with this run.")
    # S5: the permission mode; None = auto when auto_approve, else the user file's or default.
    mode: Literal["default", "accept-edits", "auto", "read-only"] | None = None


class RunStartResult(BaseModel):
    run_id: str
    run_dir: str
    session_id: str | None = None


RUN_SUBSCRIBE = "run.subscribe"


class RunSubscribeParams(_Params):
    run_id: str
    from_seq: int = Field(default=0, ge=0, description="Replay durable events from this seq.")


class RunSubscribeResult(BaseModel):
    run_id: str
    live: bool = Field(description="False if the run is not in memory and only replayed from disk.")


RUN_CANCEL = "run.cancel"


class RunCancelParams(_Params):
    run_id: str


class RunCancelResult(BaseModel):
    cancelled: bool


RUN_LIST = "run.list"


class RunListParams(_Params):
    pass


class RunInfo(BaseModel):
    run_id: str
    status: str
    goal: str
    workspace: str
    started_at: datetime
    pending_approvals: int
    plan_done: int | None = Field(default=None, description="Completed tasks; None: no plan.")
    plan_total: int | None = None
    session_id: str | None = None


class RunListResult(BaseModel):
    runs: list[RunInfo]


APPROVAL_RESPOND = "approval.respond"


class ApprovalRespondParams(_Params):
    run_id: str
    tool_use_id: str
    approve: bool
    reason: str = Field(default="", max_length=500, description="Why not; told to the model.")
    remember: bool = Field(default=False, description="Always allow this for the session.")


class ApprovalRespondResult(BaseModel):
    accepted: bool = Field(description="False if already answered (by another client) or expired.")


# ---- plans (S3): read a run's plan; steer a live one

PLAN_GET = "plan.get"


class PlanGetParams(_Params):
    run_id: str


class PlanGetResult(BaseModel):
    run_id: str
    tasks: list[PlanTask] = Field(description="Empty if the run made no plan.")
    live: bool = Field(description="False if read from a finished run's events on disk.")


PLAN_EDIT = "plan.edit"


class PlanEditParams(_Params):
    run_id: str
    add: list[NewTask] = Field(default_factory=list, max_length=20)
    changes: list[TaskChange] = Field(default_factory=list, max_length=20)


class PlanEditResult(BaseModel):
    tasks: list[PlanTask]
    summary: str = Field(description="What changed; the model is told at its next call.")


# ---- sessions (S4): runs that share a conversation

SESSION_CREATE = "session.create"
SESSION_LIST = "session.list"
SESSION_GET = "session.get"


class SessionView(SessionInfo):
    active_run_id: str | None = Field(default=None, description="Its run in progress, if any.")


class SessionCreateParams(_Params):
    workspace: str
    title: str = ""


class SessionListParams(_Params):
    workspace: str | None = Field(default=None, description="Only sessions in this directory.")


class SessionListResult(BaseModel):
    sessions: list[SessionView]


class SessionGetParams(_Params):
    session_id: str


# ---- durable notes (S4): the user's view of the agent's memory

NOTES_LIST = "notes.list"
NOTES_ADD = "notes.add"
NOTES_UPDATE = "notes.update"
NOTES_DELETE = "notes.delete"


class NotesListParams(_Params):
    workspace: str
    session_id: str | None = Field(default=None, description="Also show this session's notes.")


class NotesListResult(BaseModel):
    notes: list[Note]


class NotesAddParams(_Params):
    workspace: str
    text: str = Field(min_length=1)
    scope: NoteScope = "workspace"
    session_id: str | None = None
    source: str = ""
    volatile: bool = False


class NotesUpdateParams(_Params):
    workspace: str
    note_id: str
    session_id: str | None = None
    text: str | None = None
    source: str | None = None
    volatile: bool | None = None


class NotesDeleteParams(_Params):
    workspace: str
    note_id: str
    session_id: str | None = None


class NoteResult(BaseModel):
    note: Note


# ---- server -> client notifications (no response)

EVENT_NOTIFICATION = "run.event"  # params: {"event": <Event>}
STREAM_END_NOTIFICATION = "run.stream_end"  # params: StreamEnd


class StreamEnd(BaseModel):
    run_id: str
    reason: Literal["finished", "lagged", "replayed"] = Field(
        description="finished: run ended; lagged: client fell behind, re-subscribe from "
        "next_seq; replayed: run not live, every stored event was sent."
    )
    next_seq: int
