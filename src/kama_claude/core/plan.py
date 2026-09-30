"""A run's plan: the task list the model keeps through the task_* tools.

Planning as state, not prose: a plan written into a reply scrolls out of attention
and nothing can check it. Kept here, the runtime can show it to clients, persist it as
events, and notice when the model ends its turn with work still open.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
OPEN_STATUSES: frozenset[TaskStatus] = frozenset({"pending", "in_progress"})
MAX_TASKS = 50
# Exact names, not a `task_` prefix: an MCP server (S7) may bring its own task_* tools.
PLAN_TOOL_NAMES = frozenset({"task_create", "task_update", "task_list"})

_MARK: dict[TaskStatus, str] = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "cancelled": "[-]",
}


class PlanTask(BaseModel):
    id: int
    title: str
    status: TaskStatus = "pending"
    note: str = ""


class PlanError(ValueError):
    """A plan operation the model got wrong; the message tells it how to fix the call."""


class Plan:
    def __init__(self) -> None:
        self._tasks: list[PlanTask] = []
        self.version = 0  # bumped on every change, so the loop can tell a tool changed it

    @property
    def tasks(self) -> list[PlanTask]:
        return [t.model_copy() for t in self._tasks]

    def add(self, titles: list[str]) -> list[PlanTask]:
        if len(self._tasks) + len(titles) > MAX_TASKS:
            raise PlanError(f"a plan holds at most {MAX_TASKS} tasks; group related work")
        start = len(self._tasks) + 1
        new = [PlanTask(id=start + i, title=t.strip()) for i, t in enumerate(titles)]
        self._tasks += new
        self.version += 1
        return new

    def update(
        self, task_id: int, status: TaskStatus | None = None, note: str | None = None
    ) -> PlanTask:
        task = next((t for t in self._tasks if t.id == task_id), None)
        if task is None:
            ids = ", ".join(str(t.id) for t in self._tasks) or "none yet"
            raise PlanError(f"no task {task_id}; existing ids: {ids}")
        if status is None and note is None:
            raise PlanError("give a status, a note, or both")
        if status == "cancelled" and not (note or task.note).strip():
            raise PlanError("cancelling a task needs a note saying why")
        if status is not None:
            task.status = status
        if note is not None:
            task.note = note.strip()
        self.version += 1
        return task.model_copy()

    def open_tasks(self) -> list[PlanTask]:
        return [t.model_copy() for t in self._tasks if t.status in OPEN_STATUSES]

    def counts(self) -> dict[str, int]:
        c = {"tasks": len(self._tasks), "completed": 0, "cancelled": 0, "open": 0}
        for t in self._tasks:
            c["open" if t.status in OPEN_STATUSES else t.status] += 1
        return c

    def render(self) -> str:
        return render_tasks(self._tasks)


def render_task(t: PlanTask) -> str:
    note = f"  ({t.note})" if t.note else ""
    return f"{_MARK[t.status]} {t.id}. {t.title}{note}"


def render_tasks(tasks: list[PlanTask]) -> str:
    if not tasks:
        return "No plan yet."
    done = sum(t.status == "completed" for t in tasks)
    return "\n".join([f"Plan ({done}/{len(tasks)} completed):", *map(render_task, tasks)])
