"""task_create / task_update / task_list: the model's plan as tool calls.

Create/update by id rather than rewriting the whole list on every change: an update
costs a few tokens instead of the whole list, and a task can't silently disappear (it
has to be cancelled, with a reason). Every result echoes the full plan, so the current
state is always the latest thing the model read about it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from kama_claude.core.plan import PlanError, TaskStatus
from kama_claude.core.tools.base import Tool, ToolContext, ToolError, ToolResult

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskCreateParams(_Params):
    tasks: list[Title] = Field(
        min_length=1,
        max_length=20,
        description="Task titles in the order you plan to do them. Each one concrete and "
        "checkable, e.g. 'Make load_trades skip bad rows'.",
    )


class TaskCreate(Tool[TaskCreateParams]):
    name = "task_create"
    description = (
        "Add tasks to your plan for this goal (several at once). New tasks start as "
        "pending and get ids 1, 2, 3, ... in order. Returns the whole plan."
    )
    params_model = TaskCreateParams

    async def run(self, params: TaskCreateParams, ctx: ToolContext) -> ToolResult:
        try:
            ctx.plan.add(params.tasks)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return ToolResult(ctx.plan.render())


class TaskUpdateParams(_Params):
    id: int = Field(description="Task id from the plan.")
    status: TaskStatus | None = Field(
        default=None,
        description="in_progress when you start it; completed once it is done and "
        "verified; cancelled (with a note) if it turned out not to be needed.",
    )
    note: str | None = Field(
        default=None, max_length=300, description="Short note: why cancelled, what's left."
    )


class TaskUpdate(Tool[TaskUpdateParams]):
    name = "task_update"
    description = (
        "Change a task's status and/or note. Call it as soon as a task's state changes, "
        "not in a batch at the end. Returns the whole plan."
    )
    params_model = TaskUpdateParams

    async def run(self, params: TaskUpdateParams, ctx: ToolContext) -> ToolResult:
        try:
            ctx.plan.update(params.id, params.status, params.note)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return ToolResult(ctx.plan.render())


class TaskListParams(_Params):
    pass


class TaskList(Tool[TaskListParams]):
    name = "task_list"
    description = "Show your plan: every task with its id, status and note."
    params_model = TaskListParams

    async def run(self, params: TaskListParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ctx.plan.render())


def plan_tools() -> list[Tool[Any]]:
    return [TaskCreate(), TaskUpdate(), TaskList()]
