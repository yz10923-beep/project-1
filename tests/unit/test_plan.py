"""S3: the plan (state), the task_* tools, and the loop behaviour built on them."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from kama_claude.core.agent.loop import AgentLoop, plan_reminder
from kama_claude.core.agent.runner import build_loop
from kama_claude.core.bus.events import (
    Event,
    LLMResponseEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunStartedEvent,
    ToolFinishedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMResponse, Message, ToolCall
from kama_claude.core.plan import (
    MAX_TASKS,
    PLAN_TOOL_NAMES,
    NewTask,
    Plan,
    PlanError,
    TaskChange,
    render_details,
)
from kama_claude.core.tools.base import Tool, ToolContext, ToolResult
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.plan_tools import plan_tools
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.span import Span
from kama_claude.core.trace.tracer import Tracer
from tests.fakes import PausingProvider, ScriptedProvider, text_response, tool_response

# ---------------------------------------------------------------- plan state


def nt(title: str, *deps: int, description: str = "") -> NewTask:
    return NewTask(title=title, blocked_by=list(deps), description=description)


def ch(task_id: int, status: Any = None, note: str | None = None, **kw: Any) -> TaskChange:
    return TaskChange(id=task_id, status=status, note=note, **kw)


def test_add_numbers_tasks_in_order_and_bumps_version() -> None:
    plan = Plan()
    plan.add([nt("load data"), nt(" write report ", 1)])
    plan.add([nt("test it", 2)])
    assert [(t.id, t.title, t.status, t.blocked_by) for t in plan.tasks] == [
        (1, "load data", "pending", []),
        (2, "write report", "pending", [1]),
        (3, "test it", "pending", [2]),
    ]
    assert plan.version == 2


def test_update_rules() -> None:
    plan = Plan()
    plan.add([nt("a"), nt("b")])
    with pytest.raises(PlanError, match="no task 7; existing ids: 1, 2"):
        plan.update([ch(7, "completed")])
    with pytest.raises(PlanError, match="changes nothing"):
        plan.update([ch(1)])
    with pytest.raises(PlanError, match="needs a note"):
        plan.update([ch(2, "cancelled")])
    v = plan.version
    plan.update([ch(2, "cancelled", "not needed: already exists")])
    plan.update([ch(1, "in_progress")])
    plan.update([ch(1, "completed")])
    plan.update([ch(1, "in_progress", "tests failed again")])  # reopening is allowed
    assert plan.version == v + 4  # one bump per successful call
    assert plan.counts() == {"tasks": 2, "completed": 0, "cancelled": 1, "open": 1}
    assert [t.id for t in plan.open_tasks()] == [1]


def test_batch_is_applied_in_order_and_all_or_nothing() -> None:
    plan = Plan()
    plan.add([nt("a"), nt("b", 1), nt("c")])
    # in order: 2 may start because 1 completes earlier in the same batch
    plan.update([ch(1, "completed"), ch(2, "in_progress")])
    assert [t.status for t in plan.tasks] == ["completed", "in_progress", "pending"]
    v = plan.version
    with pytest.raises(PlanError, match="needs a note"):
        plan.update([ch(3, "in_progress"), ch(2, "cancelled")])  # second one is invalid
    assert plan.version == v and plan.tasks[2].status == "pending"  # first one rolled back


def test_blocked_tasks_cannot_start_or_complete() -> None:
    plan = Plan()
    plan.add([nt("fetch"), nt("parse", 1), nt("report", 1, 2)])
    for status in ("in_progress", "completed"):
        with pytest.raises(PlanError, match=r"task 3 is blocked by 1 \(pending\), 2 \(pending\)"):
            plan.update([ch(3, status)])
    assert [t.id for t in plan.ready()] == [1]
    plan.update([ch(1, "completed")])
    assert [t.id for t in plan.ready()] == [2]
    # a cancelled blocker counts as resolved: that work is not going to happen
    plan.update([ch(2, "cancelled", "data already parsed")])
    plan.update([ch(3, "in_progress")])
    # the model can also drop a dependency it no longer needs
    plan.add([nt("extra", 3)])
    plan.update([ch(4, remove_blocked_by=[3]), ch(4, "completed")])
    assert plan.get(4).status == "completed" and plan.get(4).blocked_by == []


@pytest.mark.parametrize(
    ("setup", "change", "error"),
    [
        ([nt("a", 1)], None, "cannot be blocked by itself"),
        ([nt("a", 5)], None, "unknown task 5"),
        ([nt("a", 2), nt("b", 1)], None, "dependency cycle: 1 -> 2 -> 1"),
        ([nt("a"), nt("b", 1)], ch(1, add_blocked_by=[2]), "dependency cycle"),
    ],
)
def test_dependency_graph_is_validated(
    setup: list[NewTask], change: TaskChange | None, error: str
) -> None:
    plan = Plan()
    if change is None:
        with pytest.raises(PlanError, match=error):
            plan.add(setup)
        assert plan.tasks == []  # nothing half-added
        return
    plan.add(setup)
    with pytest.raises(PlanError, match=error):
        plan.update([change])


def test_timestamps_track_work_on_each_task() -> None:
    plan = Plan()
    plan.add([nt("a")])
    assert plan.get(1).started_at is None
    plan.update([ch(1, "in_progress")])
    started = plan.get(1).started_at
    assert started is not None and plan.get(1).finished_at is None
    plan.update([ch(1, "completed")])
    assert plan.get(1).finished_at is not None
    plan.update([ch(1, "in_progress")])  # reopened: started_at kept, finished_at cleared
    assert plan.get(1).started_at == started and plan.get(1).finished_at is None


def test_task_limit() -> None:
    plan = Plan()
    plan.add([nt("x")] * MAX_TASKS)
    with pytest.raises(PlanError, match="at most"):
        plan.add([nt("one more")])


def test_tasks_are_copies() -> None:
    plan = Plan()
    plan.add([nt("a")])
    plan.tasks[0].status = "completed"
    plan.get(1).blocked_by.append(9)
    assert plan.tasks[0].status == "pending" and plan.get(1).blocked_by == []


def test_render() -> None:
    plan = Plan()
    assert plan.render() == "No plan yet."
    plan.add([nt("load"), nt("report", 1), nt("tests", 2), nt("docs")])
    plan.update([ch(1, "completed"), ch(2, "in_progress"), ch(4, "cancelled", "out of scope")])
    plan.add([nt("sign-off")], by="user")
    assert plan.render() == (
        "Plan (1/5 completed):\n"
        "[x] 1. load\n"
        "[>] 2. report\n"
        "[ ] 3. tests  (blocked by 2)\n"
        "[-] 4. docs  (out of scope)\n"
        "[ ] 5. sign-off  (added by the user)"
    )


def test_details() -> None:
    plan = Plan()
    plan.add([nt("fetch"), nt("parse", 1, description="csv, skip bad rows"), nt("report", 2)])
    assert render_details(plan.get(2), plan.tasks) == (
        "[ ] 2. parse  (blocked by 1)\n"
        "description: csv, skip bad rows\n"
        "blocked_by: 1 (pending)\n"
        "blocks: 3"
    )


# ---------------------------------------------------------------- tools


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(plan_tools())


async def test_tools_share_the_context_plan(registry: ToolRegistry, tmp_path: Path) -> None:
    ctx = ToolContext(tmp_path)
    r = await registry.execute(
        "task_create", {"tasks": [{"title": "load"}, {"title": "report", "blocked_by": [1]}]}, ctx
    )
    assert not r.is_error and r.content.startswith("Plan (0/2 completed)")
    assert "[ ] 2. report  (blocked by 1)" in r.content
    r = await registry.execute(
        "task_update",
        {"updates": [{"id": 1, "status": "completed"}, {"id": 2, "status": "in_progress"}]},
        ctx,
    )
    assert "[x] 1. load" in r.content and "[>] 2. report" in r.content
    r = await registry.execute("task_get", {"id": 1}, ctx)
    assert r.content == "[x] 1. load\nblocks: 2"
    r = await registry.execute("task_list", {}, ctx)
    assert r.content == ctx.plan.render()
    assert ToolContext(tmp_path).plan.tasks == []  # a new context is a new plan


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("task_create", {"tasks": []}, "Invalid input"),
        ("task_create", {"tasks": [{"title": "  "}]}, "Invalid input"),
        ("task_create", {"tasks": ["just a string"]}, "Invalid input"),
        ("task_update", {"updates": [{"id": 1, "status": "done"}]}, "Invalid input"),
        ("task_update", {"id": 1, "status": "completed"}, "Invalid input"),  # not batched
        ("task_update", {"updates": [{"id": 9, "status": "completed"}]}, "no task 9"),
        ("task_update", {"updates": [{"id": 1, "status": "cancelled"}]}, "needs a note"),
        ("task_update", {"updates": [{"id": 2, "status": "in_progress"}]}, "blocked by 1"),
        ("task_get", {"id": 3}, "no task 3"),
    ],
)
async def test_mistakes_come_back_as_errors_the_model_can_fix(
    registry: ToolRegistry, tmp_path: Path, name: str, args: dict[str, object], expected: str
) -> None:
    ctx = ToolContext(tmp_path)
    await registry.execute(
        "task_create", {"tasks": [{"title": "a"}, {"title": "b", "blocked_by": [1]}]}, ctx
    )
    r = await registry.execute(name, args, ctx)
    assert r.is_error and expected in r.content


def test_plan_tools_need_no_approval(registry: ToolRegistry) -> None:
    # Pure bookkeeping inside the run: asking a human would only add friction.
    for name in PLAN_TOOL_NAMES:
        tool = registry.get(name)
        assert tool is not None and not tool.requires_approval


# ---------------------------------------------------------------- loop


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events if e.type != "llm.delta"]

    def of[E](self, kind: type[E]) -> list[E]:
        return [e for e in self.events if isinstance(e, kind)]


class SpanList:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def write(self, span: Span) -> None:
        self.spans.append(span)


async def allow(_: ToolCall) -> bool:
    return True


def make_loop(
    provider: ScriptedProvider, ws: Path, tracer: Tracer | None = None
) -> tuple[AgentLoop, ListSink]:
    sink = ListSink()
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools() + plan_tools()),
        sink=sink,
        workspace=ws,
        approver=allow,
        tracer=tracer,
    )
    return loop, sink


def create(tid: str, *titles: str) -> tuple[str, str, dict[str, Any]]:
    return (tid, "task_create", {"tasks": [{"title": x} for x in titles]})


def update(tid: str, task_id: int, status: str, note: str | None = None) -> Any:
    change: dict[str, Any] = {"id": task_id, "status": status}
    if note:
        change["note"] = note
    return (tid, "task_update", {"updates": [change]})


def rebuild_messages(goal: str, events: list[Event]) -> list[Message]:
    """The conversation, from events.jsonl alone (the S1 invariant, now with reminders)."""
    msgs: list[Message] = [{"role": "user", "content": goal}]
    results: list[dict[str, Any]] = []
    for e in events:
        if isinstance(e, LLMResponseEvent | PlanReminderEvent) and results:
            msgs.append({"role": "user", "content": results})
            results = []
        if isinstance(e, LLMResponseEvent):
            msgs.append({"role": "assistant", "content": e.content})
        elif isinstance(e, ToolFinishedEvent):
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": e.tool_use_id,
                "content": e.output,
            }
            if e.is_error:
                block["is_error"] = True
            results.append(block)
        elif isinstance(e, PlanReminderEvent):
            msgs.append({"role": "user", "content": e.text})
        elif isinstance(e, PlanNoticeEvent):
            if results:
                msgs.append({"role": "user", "content": results})
                results = []
            last = msgs[-1]["content"]
            blocks = [{"type": "text", "text": last}] if isinstance(last, str) else list(last)
            msgs[-1] = {"role": "user", "content": [*blocks, {"type": "text", "text": e.text}]}
    return msgs


async def test_plan_is_offered_kept_and_emitted_as_snapshots(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            # plan + start the first task in one turn (parallel tool calls)
            tool_response(create("c", "load", "report"), update("u1", 1, "in_progress")),
            tool_response(update("u2", 1, "completed"), update("u3", 2, "completed")),
            tool_response(("l", "task_list", {})),  # reads only: no plan.updated
            text_response("All done."),
        ]
    )
    tracer_sink = SpanList()
    loop, sink = make_loop(p, tmp_path, Tracer("r1", tracer_sink))
    r = await loop.run("do two things", "r1")

    assert (r.status, r.final_text) == ("completed", "All done.")
    assert "task_create" in p.requests[0].system
    assert {"task_create", "task_update", "task_list"} <= {t["name"] for t in p.requests[0].tools}
    assert sink.of(RunStartedEvent)[0].planning is True
    snaps = sink.of(PlanUpdatedEvent)
    assert [s.tool_use_id for s in snaps] == ["c", "u1", "u2", "u3"]
    assert [(t.id, t.status) for t in snaps[1].tasks] == [(1, "in_progress"), (2, "pending")]
    assert all(t.status == "completed" for t in snaps[-1].tasks)
    # each snapshot sits between its tool's started and finished events
    types = sink.types()
    first = types.index("plan.updated")
    assert types[first - 1 : first + 2] == ["tool.started", "plan.updated", "tool.finished"]
    # the model reads the whole plan back in every task_* result
    last_results = p.requests[3].messages[-1]["content"]
    assert last_results[0]["content"].startswith("Plan (2/2 completed)")
    assert not sink.of(PlanReminderEvent)

    run = next(s for s in tracer_sink.spans if s.name == "run")
    assert {k: v for k, v in run.attrs.items() if k.startswith("plan_")} == {
        "plan_tasks": 2,
        "plan_completed": 2,
        "plan_cancelled": 0,
        "plan_open": 0,
        "plan_reminders": 0,
        "plan_budget_credit": 3,  # steps 1-3 only touched the plan
    }


async def test_stopping_with_open_tasks_gets_one_reminder(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(create("c", "code", "tests")),
            tool_response(update("u1", 1, "completed")),
            text_response("Done!"),  # premature: task 2 is still pending
            tool_response(update("u2", 2, "cancelled", "the spec does not ask for tests")),
            text_response("Done: code written, tests not required."),
        ]
    )
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("goal", "r1")

    assert r.status == "completed" and r.final_text.startswith("Done: code")
    [reminder] = sink.of(PlanReminderEvent)
    assert reminder.open_task_ids == [2] and "[ ] 2. tests" in reminder.text
    # the model saw the reminder as the next user turn, right after its own "Done!"
    after_stop = p.requests[3].messages
    assert after_stop[-2]["role"] == "assistant"
    assert after_stop[-1] == {"role": "user", "content": reminder.text}
    # events.jsonl still reconstructs exactly what the model was sent
    assert rebuild_messages("goal", sink.events)[:-1] == p.requests[-1].messages


async def test_reminder_is_sent_only_once(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(create("c", "a")),
            text_response("I need the user to tell me which database to use."),
            text_response("Still blocked: which database?"),
        ]
    )
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("goal", "r1")
    # A model with a reason to stop is not forced on: the run ends, the plan shows it.
    assert (r.status, r.steps) == ("completed", 3)
    assert len(sink.of(PlanReminderEvent)) == 1


async def test_no_plan_no_reminder(tmp_path: Path) -> None:
    p = ScriptedProvider([text_response("42")])
    loop, sink = make_loop(p, tmp_path)
    assert (await loop.run("what is 6*7", "r1")).steps == 1
    assert sink.types() == ["run.started", "llm.response", "run.finished"]


async def test_rejected_plan_call_emits_nothing(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(create("c", "a")),
            tool_response(update("bad", 5, "completed")),
            tool_response(update("u", 1, "completed")),
            text_response("ok"),
        ]
    )
    loop, sink = make_loop(p, tmp_path)
    await loop.run("goal", "r1")
    assert [s.tool_use_id for s in sink.of(PlanUpdatedEvent)] == ["c", "u"]
    bad = next(e for e in sink.of(ToolFinishedEvent) if e.tool_use_id == "bad")
    assert bad.is_error and "no task 5" in bad.output


async def test_each_run_starts_with_an_empty_plan(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [tool_response(create("c", "a")), text_response("x"), text_response("y")]
        + [tool_response(("l", "task_list", {})), text_response("z")]
    )
    loop, _ = make_loop(p, tmp_path)
    await loop.run("first", "r1")
    await loop.run("second", "r2")
    assert p.requests[-1].messages[-1]["content"][0]["content"] == "No plan yet."


async def test_planning_off_is_the_s2_agent(tmp_path: Path) -> None:
    """KAMA_PLANNING=false is the A/B baseline: no task tools, and the system prompt is
    byte-identical to the S2 one, so a comparison changes exactly one thing."""
    s2_prompt = (
        f"You are kama, a coding agent working inside the directory {tmp_path}.\n\n"
        "Use the tools to inspect and change files and to run commands. Relative paths "
        "resolve against that directory, and you cannot access anything outside it.\n\n"
        "Work in small, checked steps: look at the relevant files before editing, and "
        "verify your changes (run the code or the tests) when you can. Prefer one focused "
        "command over many exploratory ones.\n\n"
        "When the goal is done, reply with a short summary of what you changed and how you "
        "verified it. If you cannot finish, say exactly what blocked you."
    )
    p = ScriptedProvider([text_response("ok")])
    sink = ListSink()
    loop = build_loop(
        Settings(planning=False), workspace=tmp_path, sink=sink, approver=allow, provider=p
    )
    await loop.run("goal", "r1")
    assert p.requests[0].system == s2_prompt
    assert not {"task_create", "task_update", "task_list"} & {
        t["name"] for t in p.requests[0].tools
    }
    assert sink.of(RunStartedEvent)[0].planning is False


def test_reminder_text_lists_open_tasks() -> None:
    plan = Plan()
    plan.add([nt("a"), nt("b"), nt("c", 2)])
    plan.update([ch(1, "completed"), ch(2, "in_progress")])
    text = plan_reminder(plan.open_tasks(), plan.tasks)
    assert "[>] 2. b\n[ ] 3. c  (blocked by 2)" in text and "1. a" not in text


async def test_console_shows_checklist_then_one_line_per_change(tmp_path: Path) -> None:
    import io

    from kama_claude.core.agent.sinks import ConsolePrinter

    p = ScriptedProvider(
        [
            tool_response(create("c", "load", "report"), update("u1", 1, "in_progress")),
            tool_response(update("u2", 1, "completed"), update("bad", 9, "completed")),
            text_response("done"),
            tool_response(update("u3", 2, "cancelled", "not needed")),
            text_response("done for real"),
        ]
    )
    out = io.StringIO()
    loop = AgentLoop(
        provider=p,
        registry=ToolRegistry(plan_tools()),
        sink=ConsolePrinter(out),
        workspace=tmp_path,
        approver=allow,
    )
    await loop.run("g", "r1")
    text = re.sub(r"\d+ms", "Nms", out.getvalue())  # timings vary
    lines = [ln for ln in text.splitlines() if not ln.startswith(("[step", "  done"))]
    assert lines[1:] == [
        "  plan (0/2)",
        "    [ ] 1. load",
        "    [ ] 2. report",
        "  [>] 1. load  (0/2)",
        "  [x] 1. load  (1/2)",
        "  ← error · Nms · Error: no task 9; existing ids: 1, 2",  # mistakes stay visible
        "  ! stopped with 1 open task(s); reminding the model of its plan",
        "  [-] 2. report  (not needed)  (1/2)",
        "",
        "== completed after 5 steps · Nms · tokens in=50 out=25 cache_read=0 cache_write=0",
        "plan: 1/2 completed · 1 cancelled · 0 open",
    ]


async def test_trace_reports_the_plan_and_its_bookkeeping_cost(tmp_path: Path) -> None:
    from kama_claude.core.trace.analyze import render, summarize

    p = ScriptedProvider(
        [
            tool_response(create("c", "look", "answer")),
            tool_response(update("u1", 1, "completed"), ("l", "list_dir", {})),
            text_response("stopping early"),
            tool_response(update("u2", 2, "completed")),
            text_response("done"),
        ]
    )
    spans = SpanList()
    loop, _ = make_loop(p, tmp_path, Tracer("r1", spans))
    await loop.run("g", "r1")
    s = summarize(spans.spans)
    assert s.plan == {
        "tasks": 2,
        "completed": 2,
        "cancelled": 0,
        "open": 0,
        "reminders": 1,
        "budget_credit": 2,
    }
    assert (s.tool_calls, s.plan_tool_calls) == (4, 3)
    assert s.plan_only_steps == 2  # steps 1 and 4; step 2 also ran list_dir
    assert (
        "plan    2 tasks · 2 completed · 0 cancelled · 0 open · 1 reminder(s) · "
        "3 task_* calls (75% of tool calls)\n        2 of 5 steps only updated the plan"
    ) in render(spans.spans)


async def test_trace_has_no_plan_line_when_planning_is_off(tmp_path: Path) -> None:
    from kama_claude.core.trace.analyze import render, summarize

    spans = SpanList()
    loop = AgentLoop(
        provider=ScriptedProvider([text_response("hi")]),
        registry=ToolRegistry(builtin_tools()),
        sink=ListSink(),
        workspace=tmp_path,
        approver=allow,
        tracer=Tracer("r1", spans),
    )
    await loop.run("g", "r1")
    assert summarize(spans.spans).plan is None
    assert "\nplan " not in render(spans.spans)


def test_pre_s3_event_logs_still_parse() -> None:
    # Runs recorded before S3 have no `planning` field; replay after a restart must work.
    from kama_claude.core.bus.events import EVENT_ADAPTER

    old = (
        '{"type":"run.started","run_id":"r","seq":0,"at":"2026-09-01T00:00:00Z",'
        '"goal":"g","model":"m","workspace":"/w","max_steps":30}'
    )
    event = EVENT_ADAPTER.validate_json(old)
    assert isinstance(event, RunStartedEvent) and event.planning is False


def test_plan_tool_names_match_the_tools() -> None:
    from kama_claude.core.plan import PLAN_TOOL_NAMES

    assert {t.name for t in plan_tools()} == PLAN_TOOL_NAMES


# ---------------------------------------------------------------- step budget


def budget_loop(p: ScriptedProvider, ws: Path, max_steps: int) -> tuple[AgentLoop, ListSink]:
    sink = ListSink()
    loop = AgentLoop(
        provider=p,
        registry=ToolRegistry(builtin_tools() + plan_tools()),
        sink=sink,
        workspace=ws,
        approver=allow,
        max_steps=max_steps,
    )
    return loop, sink


async def test_plan_only_steps_do_not_use_up_the_work_budget(tmp_path: Path) -> None:
    # 6 steps with max_steps=4: plan-only steps 1 and 3 are credited back (allowance
    # 4 // 2 = 2); step 5 is plan-only too but past the allowance, so it counts.
    p = ScriptedProvider(
        [
            tool_response(create("c", "look", "answer")),  # plan-only
            tool_response(update("u1", 1, "in_progress"), ("l", "list_dir", {})),  # work
            tool_response(update("u2", 1, "completed"), update("u3", 2, "in_progress")),
            tool_response(("l2", "list_dir", {})),  # work
            tool_response(update("u4", 2, "completed")),  # plan-only, no credit left
            text_response("done"),
        ]
    )
    loop, sink = budget_loop(p, tmp_path, max_steps=4)
    r = await loop.run("g", "r1")
    [finished] = [e for e in sink.events if e.type == "run.finished"]
    assert (r.status, r.steps) == ("completed", 6)
    assert (finished.plan_only_steps, finished.budget_credit) == (3, 2)  # type: ignore[union-attr]


async def test_plan_step_allowance_is_capped(tmp_path: Path) -> None:
    # A model that only ever updates its plan still stops: max_steps + max_steps // 2.
    script: list[Any] = [tool_response(create("c", "a"))]
    script += [tool_response(update(f"u{i}", 1, "in_progress")) for i in range(10)]
    loop, sink = budget_loop(ScriptedProvider(script), tmp_path, max_steps=4)
    r = await loop.run("g", "r1")
    assert (r.status, r.steps) == ("max_steps", 6)
    assert r.error == "no final answer after 4 steps (+2 plan-only)"


async def test_no_allowance_without_planning(tmp_path: Path) -> None:
    p = ScriptedProvider([tool_response(("l", "list_dir", {}))] * 5)
    loop = AgentLoop(
        provider=p,
        registry=ToolRegistry(builtin_tools()),
        sink=ListSink(),
        workspace=tmp_path,
        approver=allow,
        max_steps=3,
    )
    r = await loop.run("g", "r1")
    assert (r.status, r.steps) == ("max_steps", 3)


# ---------------------------------------------------------------- user steering


async def test_user_edit_reaches_the_model_at_its_next_call(tmp_path: Path) -> None:
    p = PausingProvider(
        [
            tool_response(create("c", "parse", "report")),
            tool_response(update("u1", 1, "completed"), update("u2", 3, "completed")),
            text_response("done"),
        ],
        pause_at={1},
    )
    loop, sink = make_loop(p, tmp_path)
    run = asyncio.create_task(loop.run("g", "r1"))
    await p.paused.wait()  # the plan exists; the model is "thinking" about step 2
    tasks, lines = await loop.edit_plan(
        [NewTask(title="add a chart")], [ch(2, "cancelled", "the user does not need it")]
    )
    assert lines == [
        "task 2: pending -> cancelled (the user does not need it)",
        "added 3. add a chart",
    ]
    p.resume.set()
    r = await run

    assert r.status == "completed"
    [user_edit] = [e for e in sink.of(PlanUpdatedEvent) if e.by == "user"]
    assert user_edit.tool_use_id is None and user_edit.tasks[2].added_by == "user"
    # Step 2's request was already being answered, so the edit rides on step 3's.
    [notice] = sink.of(PlanNoticeEvent)
    assert notice.step == 3 and "- added 3. add a chart" in notice.text
    # appended to the (still unsent) tool_result message, after the results
    sent = p.requests[2].messages[-1]["content"]
    assert [b["type"] for b in sent] == ["tool_result", "tool_result", "text"]
    assert sent[-1]["text"] == notice.text
    # earlier turns untouched, and events.jsonl rebuilds exactly what was sent
    assert p.requests[2].messages[: len(p.requests[1].messages)] == p.requests[1].messages
    assert rebuild_messages("g", sink.events)[:-1] == p.requests[-1].messages


async def test_edit_while_a_tool_runs_is_not_credited_to_that_tool(tmp_path: Path) -> None:
    """A user edit landing mid-tool bumps the plan version; the loop must not report it
    as a change made by the model's (non-plan) tool call."""
    started, release = asyncio.Event(), asyncio.Event()

    class WaitParams(BaseModel):
        pass

    class Wait(Tool[WaitParams]):
        name = "wait"
        description = "waits"
        params_model = WaitParams

        async def run(self, params: WaitParams, ctx: ToolContext) -> ToolResult:
            started.set()
            await release.wait()
            return ToolResult("waited")

    p = ScriptedProvider(
        [
            tool_response(create("c", "a")),
            tool_response(("w", "wait", {})),
            tool_response(update("u", 1, "completed"), update("u2", 2, "completed")),
            text_response("ok"),
        ]
    )
    sink = ListSink()
    loop = AgentLoop(
        provider=p,
        registry=ToolRegistry([Wait(), *plan_tools()]),
        sink=sink,
        workspace=tmp_path,
        approver=allow,
    )
    run = asyncio.create_task(loop.run("g", "r1"))
    await started.wait()
    await loop.edit_plan([NewTask(title="b")], [])
    release.set()
    assert (await run).status == "completed"
    assert [(e.tool_use_id, e.by) for e in sink.of(PlanUpdatedEvent)] == [
        ("c", "model"),
        (None, "user"),  # not ("w", "model")
        ("u", "model"),
        ("u2", "model"),
    ]


async def test_notice_waits_for_a_user_turn_after_pause_turn(tmp_path: Path) -> None:
    paused = LLMResponse(stop_reason="pause_turn", content=[{"type": "text", "text": "..."}])
    p = PausingProvider(
        [tool_response(create("c", "a")), paused, tool_response(update("u", 1, "completed"))]
        + [text_response("ok")],
        pause_at={1},
    )
    loop, sink = make_loop(p, tmp_path)
    run = asyncio.create_task(loop.run("g", "r1"))
    await p.paused.wait()
    await loop.edit_plan([], [ch(1, note="use the new API")])
    p.resume.set()
    await run
    # Edited during step 2. Step 3 resumes the paused turn (last message is the
    # assistant's), so the notice waits for step 4's user message.
    [notice] = sink.of(PlanNoticeEvent)
    assert notice.step == 4
    assert p.requests[2].messages[-1]["role"] == "assistant"  # resumed as-is
    assert p.requests[3].messages[-1]["content"][-1]["text"] == notice.text
    assert rebuild_messages("g", sink.events)[:-1] == p.requests[-1].messages


async def test_rejected_edits(tmp_path: Path) -> None:
    p = PausingProvider([tool_response(create("c", "a", "b")), text_response("ok")], pause_at={1})
    loop, sink = make_loop(p, tmp_path)
    with pytest.raises(PlanError, match="not running"):
        await loop.edit_plan([NewTask(title="x")], [])
    run = asyncio.create_task(loop.run("g", "r1"))
    await p.paused.wait()
    with pytest.raises(PlanError, match="dependency cycle"):
        await loop.edit_plan([], [ch(1, add_blocked_by=[2]), ch(2, add_blocked_by=[1])])
    with pytest.raises(PlanError, match="nothing to change"):
        await loop.edit_plan([], [])
    p.resume.set()
    await run
    assert all(e.by == "model" for e in sink.of(PlanUpdatedEvent))  # nothing recorded
    assert not sink.of(PlanNoticeEvent)

    off = AgentLoop(
        provider=ScriptedProvider([]),
        registry=ToolRegistry(builtin_tools()),
        sink=ListSink(),
        workspace=tmp_path,
        approver=allow,
    )
    with pytest.raises(PlanError, match="planning is off"):
        await off.edit_plan([NewTask(title="x")], [])


# ---------------------------------------------------------------- time per task


async def test_each_task_is_timed_from_start_to_finish(tmp_path: Path) -> None:
    from kama_claude.core.trace.analyze import render, summarize, to_chrome

    p = PausingProvider(
        [
            tool_response(create("c", "parse", "chart", "docs"), update("u1", 1, "in_progress")),
            tool_response(update("u2", 1, "completed"), update("u3", 2, "in_progress")),
            tool_response(update("u4", 2, "cancelled", "no data"), update("u5", 3, "in_progress")),
            text_response("stopping"),  # task 3 still open -> reminder
            text_response("really stopping"),
        ],
        pause_at={2},
    )
    spans = SpanList()
    loop, _ = make_loop(p, tmp_path, Tracer("r1", spans))
    run = asyncio.create_task(loop.run("g", "r1"))
    await p.paused.wait()
    await loop.edit_plan([], [ch(2, "pending")])  # the user re-queues task 2 mid-work
    p.resume.set()
    await run

    run_span = next(s for s in spans.spans if s.name == "run")
    tasks = [s for s in spans.spans if s.kind == "plan"]
    assert all(s.parent_id == run_span.span_id for s in tasks)
    got = sorted((s.attrs["task_id"], s.attrs["outcome"], s.status) for s in tasks)
    assert got == [
        (1, "completed", "ok"),
        # the user's re-queue ended task 2's only stretch of work; the model's later
        # cancel came from pending, so there was nothing more to time
        (2, "pending", "ok"),
        (3, "in_progress", "error"),  # never finished: closed at run end
    ]
    text = render(spans.spans)
    assert "time per task (in_progress -> done)" in text
    assert "still open at the end" in text
    assert "task 1: parse" not in text.split("time per task")[0]  # not in the waterfall
    lanes = {e["tid"] for e in to_chrome(spans.spans)["traceEvents"] if e.get("cat") == "plan"}
    assert lanes == {4}
    assert len(summarize(spans.spans).task_spans) == 3
