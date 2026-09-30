"""S3: the plan (state), the task_* tools, and the loop behaviour built on them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.agent.loop import AgentLoop, plan_reminder
from kama_claude.core.agent.runner import build_loop
from kama_claude.core.bus.events import (
    Event,
    LLMResponseEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunStartedEvent,
    ToolFinishedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import Message, ToolCall
from kama_claude.core.plan import MAX_TASKS, Plan, PlanError
from kama_claude.core.tools.base import ToolContext
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.plan_tools import plan_tools
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.span import Span
from kama_claude.core.trace.tracer import Tracer
from tests.fakes import ScriptedProvider, text_response, tool_response

# ---------------------------------------------------------------- plan state


def test_add_numbers_tasks_in_order_and_bumps_version() -> None:
    plan = Plan()
    plan.add(["load data", " write report "])
    plan.add(["test it"])
    assert [(t.id, t.title, t.status) for t in plan.tasks] == [
        (1, "load data", "pending"),
        (2, "write report", "pending"),
        (3, "test it", "pending"),
    ]
    assert plan.version == 2


def test_update_rules() -> None:
    plan = Plan()
    plan.add(["a", "b"])
    with pytest.raises(PlanError, match="no task 7; existing ids: 1, 2"):
        plan.update(7, "completed")
    with pytest.raises(PlanError, match="status, a note"):
        plan.update(1)
    with pytest.raises(PlanError, match="needs a note"):
        plan.update(2, "cancelled")
    v = plan.version
    plan.update(2, "cancelled", "not needed: already exists")
    plan.update(1, "in_progress")
    plan.update(1, "completed")
    plan.update(1, "in_progress", "tests failed again")  # reopening is allowed
    assert plan.version == v + 4  # one bump per successful update
    assert plan.counts() == {"tasks": 2, "completed": 0, "cancelled": 1, "open": 1}
    assert [t.id for t in plan.open_tasks()] == [1]


def test_failed_update_changes_nothing() -> None:
    plan = Plan()
    plan.add(["a"])
    v = plan.version
    with pytest.raises(PlanError):
        plan.update(1, "cancelled")
    assert plan.version == v and plan.tasks[0].status == "pending"


def test_task_limit() -> None:
    plan = Plan()
    plan.add(["x"] * MAX_TASKS)
    with pytest.raises(PlanError, match="at most"):
        plan.add(["one more"])


def test_tasks_are_copies() -> None:
    plan = Plan()
    plan.add(["a"])
    plan.tasks[0].status = "completed"
    assert plan.tasks[0].status == "pending"


def test_render() -> None:
    plan = Plan()
    assert plan.render() == "No plan yet."
    plan.add(["load", "report", "tests", "docs"])
    plan.update(1, "completed")
    plan.update(2, "in_progress")
    plan.update(4, "cancelled", "out of scope")
    assert plan.render() == (
        "Plan (1/4 completed):\n"
        "[x] 1. load\n"
        "[>] 2. report\n"
        "[ ] 3. tests\n"
        "[-] 4. docs  (out of scope)"
    )


# ---------------------------------------------------------------- tools


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(plan_tools())


async def test_tools_share_the_context_plan(registry: ToolRegistry, tmp_path: Path) -> None:
    ctx = ToolContext(tmp_path)
    r = await registry.execute("task_create", {"tasks": ["load", "report"]}, ctx)
    assert not r.is_error and r.content.startswith("Plan (0/2 completed)")
    r = await registry.execute("task_update", {"id": 1, "status": "completed"}, ctx)
    assert "[x] 1. load" in r.content
    r = await registry.execute("task_list", {}, ctx)
    assert r.content == ctx.plan.render()
    assert ToolContext(tmp_path).plan.tasks == []  # a new context is a new plan


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        ("task_create", {"tasks": []}, "Invalid input"),
        ("task_create", {"tasks": ["  "]}, "Invalid input"),
        ("task_update", {"id": 1, "status": "done"}, "Invalid input"),  # not a status
        ("task_update", {"id": 9, "status": "completed"}, "no task 9"),
        ("task_update", {"id": 1, "status": "cancelled"}, "needs a note"),
    ],
)
async def test_mistakes_come_back_as_errors_the_model_can_fix(
    registry: ToolRegistry, tmp_path: Path, name: str, args: dict[str, object], expected: str
) -> None:
    ctx = ToolContext(tmp_path)
    await registry.execute("task_create", {"tasks": ["a"]}, ctx)
    r = await registry.execute(name, args, ctx)
    assert r.is_error and expected in r.content


def test_plan_tools_need_no_approval(registry: ToolRegistry) -> None:
    # Pure bookkeeping inside the run: asking a human would only add friction.
    for name in ("task_create", "task_update", "task_list"):
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
    return (tid, "task_create", {"tasks": list(titles)})


def update(tid: str, task_id: int, status: str, note: str | None = None) -> Any:
    args: dict[str, Any] = {"id": task_id, "status": status}
    if note:
        args["note"] = note
    return (tid, "task_update", args)


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
    plan.add(["a", "b", "c"])
    plan.update(1, "completed")
    plan.update(2, "in_progress")
    text = plan_reminder(plan.open_tasks())
    assert "[>] 2. b\n[ ] 3. c" in text and "1. a" not in text


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
    assert s.plan == {"tasks": 2, "completed": 2, "cancelled": 0, "open": 0, "reminders": 1}
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
