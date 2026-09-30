"""task_create / task_update / task_get / task_list: the model's plan as tool calls.

Create/update by id rather than rewriting the whole list on every change: an update
costs a few tokens instead of the whole list, and a task can't silently disappear (it
has to be cancelled, with a reason). Updates come in batches applied in order, so
"complete 1, start 2" is one call. Every mutating result echoes the full plan, so the
current state is always the latest thing the model read about it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from kama_claude.core.plan import NewTask, PlanError, TaskChange, TaskStatus, render_details
from kama_claude.core.tools.base import Tool, ToolContext, ToolError, ToolResult

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(max_length=1000)]
Ids = Annotated[list[int], Field(max_length=20)]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewTaskParams(_Params):
    title: Title = Field(
        description="Concrete and checkable, e.g. 'Make load_trades skip bad rows'."
    )
    description: Text = Field(default="", description="Optional detail: approach, acceptance.")
    blocked_by: Ids = Field(
        default_factory=list,
        description="Ids of tasks that must be completed first (may name tasks created in "
        "this same call).",
    )


class TaskCreateParams(_Params):
    tasks: list[NewTaskParams] = Field(
        min_length=1, max_length=20, description="Tasks in the order you plan to do them."
    )


class TaskCreate(Tool[TaskCreateParams]):
    name = "task_create"
    description = (
        "Add tasks to your plan (several at once). Ids continue from the last task "
        "(a new plan starts at 1), in the order given, so blocked_by can refer to tasks "
        "in the same call. New tasks are pending. Returns the whole plan."
    )
    params_model = TaskCreateParams

    async def run(self, params: TaskCreateParams, ctx: ToolContext) -> ToolResult:
        try:
            ctx.plan.add([NewTask(**t.model_dump()) for t in params.tasks])
        except PlanError as e:
            raise ToolError(str(e)) from e
        return ToolResult(ctx.plan.render())


class TaskChangeParams(_Params):
    id: int = Field(description="Task id from the plan.")
    status: TaskStatus | None = Field(
        default=None,
        description="in_progress when you start it; completed once it is done and "
        "verified; cancelled (needs a note) if it turned out not to be needed.",
    )
    note: Text | None = Field(default=None, description="Short note: why cancelled, what's left.")
    title: Title | None = None
    description: Text | None = None
    add_blocked_by: Ids = Field(default_factory=list)
    remove_blocked_by: Ids = Field(default_factory=list)


class TaskUpdateParams(_Params):
    updates: list[TaskChangeParams] = Field(
        min_length=1,
        max_length=20,
        description="Changes applied in order, all or nothing, e.g. complete 1 then start 2.",
    )


class TaskUpdate(Tool[TaskUpdateParams]):
    name = "task_update"
    description = (
        "Change tasks: status, note, title, description or dependencies. A task cannot "
        "start or complete while a task it is blocked by is still open. Update as soon as "
        "a task's state changes, ideally in the same turn as your next piece of work. "
        "Returns the whole plan."
    )
    params_model = TaskUpdateParams

    async def run(self, params: TaskUpdateParams, ctx: ToolContext) -> ToolResult:
        try:
            ctx.plan.update([TaskChange(**u.model_dump()) for u in params.updates])
        except PlanError as e:
            raise ToolError(str(e)) from e
        return ToolResult(ctx.plan.render())


class TaskGetParams(_Params):
    id: int = Field(description="Task id from the plan.")


class TaskGet(Tool[TaskGetParams]):
    name = "task_get"
    description = "Show one task in full: description, note, what blocks it and what it blocks."
    params_model = TaskGetParams

    async def run(self, params: TaskGetParams, ctx: ToolContext) -> ToolResult:
        try:
            task = ctx.plan.get(params.id)
        except PlanError as e:
            raise ToolError(str(e)) from e
        return ToolResult(render_details(task, ctx.plan.tasks))


class TaskListParams(_Params):
    pass


class TaskList(Tool[TaskListParams]):
    name = "task_list"
    description = "Show your plan: every task with its id, status, blockers and note."
    params_model = TaskListParams

    async def run(self, params: TaskListParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ctx.plan.render())


def plan_tools() -> list[Tool[Any]]:
    return [TaskCreate(), TaskUpdate(), TaskGet(), TaskList()]
