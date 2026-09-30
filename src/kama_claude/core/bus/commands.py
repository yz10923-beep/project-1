"""Command contract: one params model and one result model per JSON-RPC method.

Adding a command = add both models here, register a handler in CoreApp,
and add a client call. The method name is the only routing key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.plan import NewTask, PlanTask, TaskChange


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


# ---- runs

RUN_START = "run.start"


class RunStartParams(_Params):
    goal: str = Field(min_length=1)
    workspace: str = Field(description="Absolute path of the directory the agent works in.")
    model: str | None = None
    max_steps: int | None = Field(default=None, ge=1)
    auto_approve: bool = Field(default=False, description="Approve bash/write_file without asking.")


class RunStartResult(BaseModel):
    run_id: str
    run_dir: str


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


class RunListResult(BaseModel):
    runs: list[RunInfo]


APPROVAL_RESPOND = "approval.respond"


class ApprovalRespondParams(_Params):
    run_id: str
    tool_use_id: str
    approve: bool


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
