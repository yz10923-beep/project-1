"""A run's plan: the task graph the model keeps through the task_* tools (and the user
can steer over IPC).

Planning as state, not prose: a plan written into a reply scrolls out of attention and
nothing can check it. Kept here, the runtime can show it to clients, persist it as
events, enforce the order the model itself declared (blocked_by), time each task, and
notice when the model ends its turn with work still open.

Every mutation is atomic: a batch of changes is applied to a copy and committed only if
all of them are valid, so a rejected call never leaves a half-applied plan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
OPEN_STATUSES: frozenset[TaskStatus] = frozenset({"pending", "in_progress"})
MAX_TASKS = 50
# Exact names, not a `task_` prefix: an MCP server (S7) may bring its own task_* tools.
PLAN_TOOL_NAMES = frozenset({"task_create", "task_update", "task_get", "task_list"})
ChangedBy = Literal["model", "user"]

_MARK: dict[TaskStatus, str] = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "cancelled": "[-]",
}


class PlanTask(BaseModel):
    id: int
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    note: str = ""
    blocked_by: list[int] = Field(default_factory=list)
    added_by: ChangedBy = "model"
    started_at: datetime | None = None  # first time it went in_progress
    finished_at: datetime | None = None  # completed or cancelled (cleared if reopened)


class NewTask(BaseModel):
    title: str
    description: str = ""
    blocked_by: list[int] = Field(default_factory=list)


class TaskChange(BaseModel):
    id: int
    status: TaskStatus | None = None
    note: str | None = None
    title: str | None = None
    description: str | None = None
    add_blocked_by: list[int] = Field(default_factory=list)
    remove_blocked_by: list[int] = Field(default_factory=list)


class PlanError(ValueError):
    """A plan operation that was rejected; the message says how to fix the call."""


def _now() -> datetime:
    return datetime.now(UTC)


class Plan:
    def __init__(self) -> None:
        self._tasks: dict[int, PlanTask] = {}
        self.version = 0  # bumped on every change, so the loop can tell a tool changed it

    @property
    def tasks(self) -> list[PlanTask]:
        return [t.model_copy(deep=True) for t in self._tasks.values()]

    def get(self, task_id: int) -> PlanTask:
        return self._find(self._tasks, task_id).model_copy(deep=True)

    # ------------------------------------------------------------------ changes

    def add(self, new: list[NewTask], by: ChangedBy = "model") -> list[PlanTask]:
        """Add tasks with ids continuing from the last one. blocked_by may name tasks
        added in the same call."""
        if not new:
            raise PlanError("give at least one task")
        if len(self._tasks) + len(new) > MAX_TASKS:
            raise PlanError(f"a plan holds at most {MAX_TASKS} tasks; group related work")
        draft = self._copy()
        start = max(draft, default=0) + 1
        added = []
        for i, spec in enumerate(new):
            title = spec.title.strip()
            if not title:
                raise PlanError("a task needs a non-empty title")
            task = PlanTask(
                id=start + i,
                title=title,
                description=spec.description.strip(),
                blocked_by=sorted(set(spec.blocked_by)),
                added_by=by,
            )
            draft[task.id] = task
            added.append(task)
        for task in added:
            self._check_deps(draft, task)
        self._check_acyclic(draft)
        self._commit(draft)
        return [t.model_copy(deep=True) for t in added]

    def update(self, changes: list[TaskChange]) -> list[PlanTask]:
        """Apply changes in order (so "complete 1, start 2" works in one call); all or
        nothing."""
        if not changes:
            raise PlanError("give at least one change")
        draft = self._copy()
        touched: list[int] = []
        for ch in changes:
            task = self._find(draft, ch.id)
            if (
                ch.status is None
                and ch.note is None
                and ch.title is None
                and ch.description is None
                and not ch.add_blocked_by
                and not ch.remove_blocked_by
            ):
                raise PlanError(f"change for task {ch.id} changes nothing")
            if ch.title is not None:
                if not ch.title.strip():
                    raise PlanError(f"task {ch.id}: title cannot be empty")
                task.title = ch.title.strip()
            if ch.description is not None:
                task.description = ch.description.strip()
            if ch.note is not None:
                task.note = ch.note.strip()
            if ch.add_blocked_by or ch.remove_blocked_by:
                deps = (set(task.blocked_by) | set(ch.add_blocked_by)) - set(ch.remove_blocked_by)
                task.blocked_by = sorted(deps)
                self._check_deps(draft, task)
            if ch.status is not None:
                self._set_status(draft, task, ch.status)
            touched.append(task.id)
        self._check_acyclic(draft)
        self._commit(draft)
        return [draft[i].model_copy(deep=True) for i in dict.fromkeys(touched)]

    def apply(self, add: list[NewTask], changes: list[TaskChange], by: ChangedBy) -> None:
        """Add tasks and change tasks as one atomic edit (a user steering the plan)."""
        if not add and not changes:
            raise PlanError("nothing to change")
        saved, version = self._copy(), self.version
        try:
            if add:
                self.add(add, by)
            if changes:
                self.update(changes)
        except PlanError:
            self._tasks, self.version = saved, version
            raise
        self.version = version + 1

    def _set_status(self, draft: dict[int, PlanTask], task: PlanTask, status: TaskStatus) -> None:
        if status == "cancelled" and not task.note:
            raise PlanError(f"cancelling task {task.id} needs a note saying why")
        if status in ("in_progress", "completed") and (
            blockers := self._open_blockers(draft, task)
        ):
            names = ", ".join(f"{b.id} ({b.status})" for b in blockers)
            raise PlanError(
                f"task {task.id} is blocked by {names}; finish those first, or remove the "
                "dependency with remove_blocked_by if it no longer applies"
            )
        if status == "in_progress" and task.started_at is None:
            task.started_at = _now()
        if status in ("completed", "cancelled"):
            task.finished_at = _now()
        elif task.status in ("completed", "cancelled"):
            task.finished_at = None  # reopened
        task.status = status

    # ------------------------------------------------------------------ queries

    def open_tasks(self) -> list[PlanTask]:
        return [t.model_copy(deep=True) for t in self._tasks.values() if t.status in OPEN_STATUSES]

    def ready(self) -> list[PlanTask]:
        """Pending tasks whose blockers are all resolved: what can be started now."""
        return [
            t.model_copy(deep=True)
            for t in self._tasks.values()
            if t.status == "pending" and not self._open_blockers(self._tasks, t)
        ]

    def counts(self) -> dict[str, int]:
        c = {"tasks": len(self._tasks), "completed": 0, "cancelled": 0, "open": 0}
        for t in self._tasks.values():
            c["open" if t.status in OPEN_STATUSES else t.status] += 1
        return c

    def render(self) -> str:
        return render_tasks(list(self._tasks.values()))

    # ------------------------------------------------------------------ internals

    def _copy(self) -> dict[int, PlanTask]:
        return {i: t.model_copy(deep=True) for i, t in self._tasks.items()}

    def _commit(self, draft: dict[int, PlanTask]) -> None:
        self._tasks = draft
        self.version += 1

    @staticmethod
    def _find(tasks: dict[int, PlanTask], task_id: int) -> PlanTask:
        if task_id not in tasks:
            ids = ", ".join(map(str, tasks)) or "none yet"
            raise PlanError(f"no task {task_id}; existing ids: {ids}")
        return tasks[task_id]

    @staticmethod
    def _check_deps(tasks: dict[int, PlanTask], task: PlanTask) -> None:
        for dep in task.blocked_by:
            if dep == task.id:
                raise PlanError(f"task {task.id} cannot be blocked by itself")
            if dep not in tasks:
                raise PlanError(f"task {task.id}: blocked_by names unknown task {dep}")

    @staticmethod
    def _open_blockers(tasks: dict[int, PlanTask], task: PlanTask) -> list[PlanTask]:
        # A cancelled blocker counts as resolved: that work is not going to happen.
        return [tasks[d] for d in task.blocked_by if tasks[d].status in OPEN_STATUSES]

    @staticmethod
    def _check_acyclic(tasks: dict[int, PlanTask]) -> None:
        state: dict[int, int] = {}  # 1 = on the current path, 2 = done

        def visit(i: int, path: list[int]) -> None:
            if state.get(i) == 2:
                return
            if state.get(i) == 1:
                cycle = " -> ".join(map(str, [*path[path.index(i) :], i]))
                raise PlanError(f"dependency cycle: {cycle}")
            state[i] = 1
            for dep in tasks[i].blocked_by:
                visit(dep, [*path, i])
            state[i] = 2

        for i in tasks:
            visit(i, [])


def open_blocker_ids(task: PlanTask, tasks: list[PlanTask]) -> list[int]:
    status = {t.id: t.status for t in tasks}
    return [d for d in task.blocked_by if status.get(d) in OPEN_STATUSES]


def render_task(t: PlanTask, tasks: list[PlanTask] | None = None) -> str:
    extra = []
    if tasks is not None and t.status in OPEN_STATUSES and (ids := open_blocker_ids(t, tasks)):
        extra.append("blocked by " + ", ".join(map(str, ids)))
    if t.note:
        extra.append(t.note)
    if t.added_by == "user":
        extra.append("added by the user")
    suffix = f"  ({'; '.join(extra)})" if extra else ""
    return f"{_MARK[t.status]} {t.id}. {t.title}{suffix}"


def render_tasks(tasks: list[PlanTask]) -> str:
    if not tasks:
        return "No plan yet."
    done = sum(t.status == "completed" for t in tasks)
    lines = [f"Plan ({done}/{len(tasks)} completed):", *(render_task(t, tasks) for t in tasks)]
    return "\n".join(lines)


def describe_changes(before: list[PlanTask], after: list[PlanTask]) -> list[str]:
    """Human-readable diff between two plans, one line per change."""
    old = {t.id: t for t in before}
    lines = []
    for t in after:
        prev = old.get(t.id)
        if prev is None:
            deps = f" (after {', '.join(map(str, t.blocked_by))})" if t.blocked_by else ""
            lines.append(f"added {t.id}. {t.title}{deps}")
            continue
        if t.status != prev.status:
            why = f" ({t.note})" if t.note and t.note != prev.note else ""
            lines.append(f"task {t.id}: {prev.status} -> {t.status}{why}")
        elif t.note != prev.note:
            lines.append(f"task {t.id}: note: {t.note}")
        if t.title != prev.title:
            lines.append(f"task {t.id}: renamed to {t.title!r}")
        if t.description != prev.description:
            lines.append(f"task {t.id}: description changed")
        if t.blocked_by != prev.blocked_by:
            lines.append(f"task {t.id}: blocked_by {prev.blocked_by} -> {t.blocked_by}")
    return lines


def render_details(t: PlanTask, tasks: list[PlanTask]) -> str:
    """Everything about one task (task_get)."""
    lines = [render_task(t, tasks)]
    if t.description:
        lines.append(f"description: {t.description}")
    if t.blocked_by:
        status = {x.id: x.status for x in tasks}
        lines.append(
            "blocked_by: " + ", ".join(f"{d} ({status.get(d, '?')})" for d in t.blocked_by)
        )
    blocks = [x.id for x in tasks if t.id in x.blocked_by]
    if blocks:
        lines.append("blocks: " + ", ".join(map(str, blocks)))
    return "\n".join(lines)
