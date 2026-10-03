"""The TUI, driven with Textual's Pilot against an in-process kama-core over real sockets:
start a run from the goal box, approve in a modal, steer the plan, stop a run, switch
runs, reattach to a finished run, and survive a dropped connection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Input, Static

from kama_claude.core.app import CoreApp
from kama_claude.core.bus.commands import (
    APPROVAL_RESPOND,
    RUN_START,
    ApprovalRespondParams,
    ApprovalRespondResult,
    RunStartParams,
    RunStartResult,
)
from kama_claude.core.bus.events import (
    LLMResponseEvent,
    RunStartedEvent,
    ToolApprovalRequestedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMProvider, Usage
from kama_claude.core.transport.client import JsonRpcClient
from kama_claude.tui.app import (
    ApprovalScreen,
    FormScreen,
    KamaTui,
    RunsScreen,
    ToolBlock,
    TraceScreen,
)
from kama_claude.tui.state import RunView
from tests.fakes import PausingProvider, ScriptedProvider, text_response, tool_response

# ---------------------------------------------------------------- RunView (pure)

AT = datetime(2026, 1, 1, tzinfo=UTC)


def started(seq: int = 0) -> RunStartedEvent:
    return RunStartedEvent(
        run_id="r", seq=seq, at=AT, goal="g", model="claude-opus-5", workspace="/w", max_steps=5
    )


def response(seq: int, model: str = "claude-opus-5") -> LLMResponseEvent:
    return LLMResponseEvent(
        run_id="r",
        seq=seq,
        at=AT,
        step=seq,
        stop_reason="end_turn",
        content=[],
        usage=Usage(input_tokens=1_000_000, output_tokens=0),
        latency_ms=1,
        model=model,
    )


def test_view_skips_events_it_already_has() -> None:
    view = RunView("r")
    assert view.apply(started()) and view.status == "running"
    assert view.apply(response(1)) and view.next_seq == 2
    assert not view.apply(response(1))  # the replay after a reconnect overlaps
    assert view.step == 1 and view.usage.input_tokens == 1_000_000


def test_view_cost_is_unknown_once_any_call_is_unpriced() -> None:
    view = RunView("r")
    view.apply(started())
    view.apply(response(1))
    assert view.cost_usd == pytest.approx(5.0)  # 1M input tokens of claude-opus-5
    view.apply(response(2, model="some-new-model"))
    assert view.cost_usd is None
    assert "cost unknown" in view.headline(auto_approve=False, connection="live")


def test_view_tracks_pending_approvals() -> None:
    view = RunView("r")
    view.apply(started())
    view.apply(
        ToolApprovalRequestedEvent(
            run_id="r", seq=1, at=AT, step=1, tool_use_id="t", name="bash", input={}
        )
    )
    assert "1 awaiting approval" in view.headline(auto_approve=False, connection="live")


# ---------------------------------------------------------------- the app, via Pilot


@dataclass
class Rig:
    core: CoreApp
    port: int
    ws: Path
    providers: list[ScriptedProvider] = field(default_factory=list)
    clients: list[JsonRpcClient] = field(default_factory=list)

    def client(self, name: str = "kama-tui") -> JsonRpcClient:
        c = JsonRpcClient("127.0.0.1", self.port, token=self.core.token, client_name=name)
        self.clients.append(c)
        return c

    def tui(self, **kw: Any) -> KamaTui:
        return KamaTui(self.client, self.ws, reconnect_delay_s=0.05, **kw)


@pytest.fixture
async def rig_factory(tmp_path: Path) -> AsyncIterator[Callable[..., Any]]:
    cores: list[CoreApp] = []

    async def make(script: Callable[[], ScriptedProvider]) -> Rig:
        ws = tmp_path / "ws"
        ws.mkdir(exist_ok=True)
        providers: list[ScriptedProvider] = []

        def factory(_: Settings) -> LLMProvider:
            providers.append(script())
            return providers[-1]

        core = CoreApp(Settings(port=0, runs_dir=tmp_path / "runs"), provider_factory=factory)
        _, port = await core.server.start()
        cores.append(core)
        return Rig(core, port, ws, providers)

    yield make
    for core in cores:
        await core.runs.shutdown()
        await core.server.stop()


async def until(pilot: Pilot[Any], cond: Callable[[], bool], within: float = 10) -> None:
    """Wait for a condition while the app keeps processing (a readiness signal, not a
    fixed sleep)."""
    async with asyncio.timeout(within):
        while not cond():
            await pilot.pause(0.02)


def log_text(app: KamaTui) -> str:
    return "\n".join(str(s.content) for s in app.query("#log Static"))


def panel(app: KamaTui, selector: str) -> str:
    return str(app.query_one(selector, Static).content)


def planned_script() -> ScriptedProvider:
    return ScriptedProvider(
        [
            tool_response(
                ("c", "task_create", {"tasks": [{"title": "look"}, {"title": "answer"}]}),
                ("u", "task_update", {"updates": [{"id": 1, "status": "in_progress"}]}),
                text="Let me look around.",
            ),
            tool_response(
                ("l", "list_dir", {}),
                ("u2", "task_update", {"updates": [{"id": 1, "status": "completed"}]}),
            ),
            tool_response(("u3", "task_update", {"updates": [{"id": 2, "status": "completed"}]})),
            text_response("All done here."),
        ]
    )


async def test_goal_box_starts_a_run_and_shows_it_live(rig_factory: Any) -> None:
    rig = await rig_factory(planned_script)
    app = rig.tui()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.click("#goal")
        await pilot.press(*"look around", "enter")
        await until(pilot, lambda: app.view is not None and app.view.finished)
        await pilot.pause()
        text = log_text(app)
        assert "▶ " in text and "look around" in text  # run header with the goal
        assert "Let me look around." in text  # streamed deltas
        assert "All done here." in text
        assert "■ completed after 4 steps" in text
        assert text.count("── step") == 4
        # streamed text sits under its own step (deltas arrive before llm.response)
        assert text.index("── step 1") < text.index("Let me look around.")
        assert text.index("── step 4") < text.index("All done here.")
        assert "── step 4 · end_turn" in text  # divider completed with the step's stats
        tools = list(app.query(ToolBlock))
        assert [t.tool_name for t in tools] == ["list_dir"]  # plan tools live in the panel
        assert tools[0].title.startswith("✔ list_dir")
        plan = panel(app, "#plan")
        assert "2/2 completed" in plan and "✔ 1. look" in plan
        assert "completed · step 4" in panel(app, "#status")
        assert "plan 2/2" in panel(app, "#status")


async def test_approval_modal_allows_and_denies(rig_factory: Any) -> None:
    rig = await rig_factory(
        lambda: ScriptedProvider(
            [
                tool_response(("b1", "bash", {"command": "echo hi > out.txt"})),
                tool_response(("b2", "bash", {"command": "rm out.txt"})),
                text_response("ok"),
            ]
        )
    )
    app = rig.tui(goal="make a file")
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        assert isinstance(app.screen, ApprovalScreen)
        assert app.screen.event.input["command"] == "echo hi > out.txt"
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        await pilot.press("n")
        await until(pilot, lambda: app.view is not None and app.view.finished)
        assert (rig.ws / "out.txt").read_text() == "hi\n"  # allowed, then rm denied
        titles = [t.title for t in app.query(ToolBlock)]
        assert "ok ·" in titles[0] and "denied" in titles[1]


async def test_approval_answered_elsewhere_closes_the_modal(rig_factory: Any) -> None:
    rig = await rig_factory(
        lambda: ScriptedProvider(
            [tool_response(("b", "bash", {"command": "touch ok.txt"})), text_response("ok")]
        )
    )
    app = rig.tui(goal="g")
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        assert app.view is not None
        async with rig.client("cli") as other:
            await other.call(
                APPROVAL_RESPOND,
                ApprovalRespondParams(run_id=app.view.run_id, tool_use_id="b", approve=True),
                ApprovalRespondResult,
            )
        await until(pilot, lambda: app.view is not None and app.view.finished)
        assert not isinstance(app.screen, ApprovalScreen)


async def test_user_steers_the_plan_from_the_tui(rig_factory: Any) -> None:
    def script() -> ScriptedProvider:
        return PausingProvider(
            [
                tool_response(("c", "task_create", {"tasks": [{"title": "a"}, {"title": "b"}]})),
                tool_response(
                    (
                        "u",
                        "task_update",
                        {
                            "updates": [
                                {"id": 1, "status": "completed"},
                                {"id": 3, "status": "completed"},
                            ]
                        },
                    )
                ),
                text_response("done"),
            ],
            pause_at={1},
        )

    rig = await rig_factory(script)
    app = rig.tui(goal="g", auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: bool(rig.providers))
        provider = rig.providers[0]
        assert isinstance(provider, PausingProvider)
        await until(pilot, lambda: provider.paused.is_set() and "0/2" in panel(app, "#plan"))

        await pilot.press("ctrl+t")  # add a task after task 1
        await until(pilot, lambda: isinstance(app.screen, FormScreen))
        await pilot.press(*"summarize", "enter", "1", "enter")
        await until(pilot, lambda: "(you)" in panel(app, "#plan"))
        await pilot.press("ctrl+o")  # cancel task 2
        await until(pilot, lambda: isinstance(app.screen, FormScreen))
        await pilot.press("2", "enter", *"not needed", "enter")
        await until(pilot, lambda: "not needed" in panel(app, "#plan"))

        provider.resume.set()
        await until(pilot, lambda: app.view is not None and app.view.finished)
        text = log_text(app)
        assert "✎ plan changed: added 3. summarize (after 1)" in text
        assert "✎ plan changed: task 2: pending -> cancelled (not needed)" in text
        assert "(the model has been told about the plan change)" in text
        sent = provider.requests[2].messages[-1]["content"][-1]["text"]
        assert "- added 3. summarize (after 1)" in sent and "- task 2: pending" in sent


async def test_stop_run_and_switch_between_runs(rig_factory: Any) -> None:
    rig = await rig_factory(lambda: PausingProvider([text_response("first")], pause_at={0}))
    app = rig.tui(goal="run one", auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.view is not None and app.view.status == "running")
        first = app.view.run_id if app.view else ""
        await pilot.press("ctrl+s")
        await until(pilot, lambda: app.view is not None and app.view.status == "cancelled")

        async with rig.client("cli") as other:  # a second run, started elsewhere
            second = (
                await other.call(
                    RUN_START,
                    RunStartParams(goal="run two", workspace=str(rig.ws), auto_approve=True),
                    RunStartResult,
                )
            ).run_id
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, RunsScreen))
        await pilot.press("enter")  # newest first: run two
        await until(pilot, lambda: app.view is not None and app.view.run_id == second)
        assert "run two" in log_text(app) and first not in log_text(app)


async def test_reattaching_a_finished_run_replays_without_prompting(rig_factory: Any) -> None:
    rig = await rig_factory(
        lambda: ScriptedProvider(
            [tool_response(("b", "bash", {"command": "true"})), text_response("finished text")]
        )
    )
    async with rig.client("cli") as c:
        run_id = (
            await c.call(
                RUN_START,
                RunStartParams(goal="g", workspace=str(rig.ws), auto_approve=True),
                RunStartResult,
            )
        ).run_id
    handle = rig.core.runs.runs[run_id]
    assert handle.task is not None
    await handle.task

    app = rig.tui(run_id=run_id)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.view is not None and app.view.finished)
        await pilot.pause(0.2)  # longer than the approval debounce
        assert not isinstance(app.screen, ApprovalScreen)  # approved long ago
        assert "finished text" in log_text(app)  # rebuilt from llm.response (no deltas)


async def test_dropped_connection_resumes_without_gaps_or_repeats(rig_factory: Any) -> None:
    rig = await rig_factory(
        lambda: PausingProvider(
            [tool_response(("l", "list_dir", {})), text_response("after the drop")],
            pause_at={1},
        )
    )
    app = rig.tui(goal="g", auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: bool(rig.providers))
        provider = rig.providers[0]
        assert isinstance(provider, PausingProvider)
        # the run can reach its pause before the TUI's watch has connected: wait for both
        await until(
            pilot,
            lambda: (
                provider.paused.is_set()
                and app.connection == "live"
                and "── step 1" in log_text(app)
            ),
        )
        before = len(rig.clients)
        for c in list(rig.clients):  # drop every connection the TUI holds
            await c.close()
        await until(pilot, lambda: len(rig.clients) > before and app.connection == "live")
        provider.resume.set()
        await until(pilot, lambda: app.view is not None and app.view.finished)
        text = log_text(app)
        assert text.count("── step 1") == 1 and text.count("── step 2") == 1
        assert app.view is not None and text.count(f"▶ {app.view.run_id}") == 1  # one header
        assert "after the drop" in text


async def test_trace_view_shows_the_report_for_the_watched_run(rig_factory: Any) -> None:
    from kama_claude.core.agent.runner import TRACE_FILE
    from kama_claude.core.trace.analyze import load_spans, render

    rig = await rig_factory(planned_script)
    runs_dir = rig.core.settings.runs_dir

    def report(run_id: str) -> str | None:
        path = runs_dir / run_id / TRACE_FILE
        return render(load_spans(path)) if path.is_file() else None

    app = KamaTui(rig.client, rig.ws, goal="g", auto_approve=True, trace_report=report)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.view is not None and app.view.finished)
        await pilot.press("ctrl+g")
        await until(pilot, lambda: isinstance(app.screen, TraceScreen))
        text = str(app.screen.query_one("#report", Static).content)
        assert "where the time went" in text and "plan    2 tasks" in text
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, TraceScreen))


async def test_typing_a_goal_during_a_live_run_asks_what_it_means(rig_factory: Any) -> None:
    """Typing into the goal box mid-run used to start a second run (seen on the VM);
    now it asks: add a task to this run, or start a new one."""
    from kama_claude.tui.app import ChoiceScreen

    def script() -> ScriptedProvider:
        return PausingProvider(
            [
                tool_response(("c", "task_create", {"tasks": [{"title": "a"}]})),
                text_response("done"),
                text_response("done again"),
            ],
            pause_at={1},
        )

    rig = await rig_factory(script)
    app = rig.tui(goal="g", auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: bool(rig.providers) and "0/1" in panel(app, "#plan"))
        first = app.view.run_id if app.view else ""
        await pilot.click("#goal")
        await pilot.press(*"check closed positions", "enter")
        await until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
        await pilot.press("t")
        await until(pilot, lambda: "check closed positions" in panel(app, "#plan"))
        assert app.view is not None and app.view.run_id == first  # no second run
        rig.providers[0].resume.set()  # type: ignore[attr-defined]
        await until(pilot, lambda: app.view is not None and app.view.finished)


async def test_unreachable_daemon_says_how_to_start_it(tmp_path: Path) -> None:
    from tests.conftest import free_port

    app = KamaTui(lambda: JsonRpcClient("127.0.0.1", free_port(), token="x"), tmp_path, goal="g")
    seen: list[str] = []
    app.notify = lambda message, **kw: seen.append(message)  # type: ignore[method-assign]
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: bool(seen))
    assert "could not start the run" in seen[0] and "uv run kama-core" in seen[0]


# ---------------------------------------------------------------- S4: conversations, memory


async def test_goals_continue_one_conversation_until_ctrl_n(rig_factory: Any) -> None:
    replies = iter(["first answer", "second answer", "fresh answer"])
    rig = await rig_factory(lambda: ScriptedProvider([text_response(next(replies))]))
    app = rig.tui(auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.click("#goal")
        await pilot.press(*"one", "enter")
        await until(pilot, lambda: app.view is not None and app.view.finished)
        sid = app.session_id
        assert sid is not None and f"session {sid}" in panel(app, "#status")
        await pilot.press(*"two", "enter")
        await until(pilot, lambda: "second answer" in log_text(app) and app.view.finished)  # type: ignore[union-attr]
        text = log_text(app)
        assert "first answer" in text  # the earlier run stays on screen
        assert "↳ continues the conversation (2 messages)" in text
        assert app.session_id == sid
        assert [m["role"] for m in rig.providers[1].requests[0].messages] == [
            "user",
            "assistant",
            "user",
        ]

        await pilot.press("ctrl+n")
        await pilot.press(*"three", "enter")
        await until(pilot, lambda: "fresh answer" in log_text(app) and app.view.finished)  # type: ignore[union-attr]
        assert app.session_id not in (None, sid)
        assert "first answer" not in log_text(app)  # a new conversation, a clean log
        assert len(rig.providers[2].requests[0].messages) == 1


async def test_memory_panel_follows_the_agents_notes(rig_factory: Any) -> None:
    from kama_claude.tui.app import NotesScreen

    rig = await rig_factory(
        lambda: ScriptedProvider(
            [
                tool_response(("n", "note_save", {"text": "tests need RISK_DB", "volatile": True})),
                text_response("saved"),
            ]
        )
    )
    app = rig.tui(goal="learn", auto_approve=True)
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: app.view is not None and app.view.finished)
        await until(pilot, lambda: "[w1] volatile tests need RISK_DB" in panel(app, "#memory"))
        assert "✎ note w1 added: tests need RISK_DB" in log_text(app)
        assert "note_save" not in " ".join(t.title for t in app.query(ToolBlock))

        await pilot.press("ctrl+l")
        await until(pilot, lambda: isinstance(app.screen, NotesScreen))
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, FormScreen))
        await pilot.press(*"reports due 17:00", "enter", "enter")
        await until(pilot, lambda: "reports due 17:00" in panel(app, "#memory"))
        assert "(you)" in panel(app, "#memory")
        await until(pilot, lambda: isinstance(app.screen, NotesScreen))
        await pilot.press("d")  # cursor on the first row: w1
        await until(pilot, lambda: "RISK_DB" not in panel(app, "#memory"))
        await pilot.press("escape")


async def test_approval_modal_shows_the_risk_and_answers_always_or_with_a_reason(
    rig_factory: Any,
) -> None:
    rig = await rig_factory(
        lambda: ScriptedProvider(
            [
                tool_response(("b1", "bash", {"command": "python -m pytest -q"})),
                tool_response(("b2", "bash", {"command": "python -m pytest -x"})),  # remembered
                tool_response(("b3", "bash", {"command": "rm -rf build"})),
                tool_response(("b4", "bash", {"command": "rm -rf .git"})),  # never asked
                text_response("ok"),
            ]
        )
    )
    app = rig.tui(goal="test and clean")
    async with app.run_test(size=(140, 40)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
        screen = app.screen
        assert isinstance(screen, ApprovalScreen)
        assert (screen.event.risk, screen.event.remember) == ("medium", ["bash: python -m pytest"])
        await pilot.press("a")
        await until(
            pilot,
            lambda: isinstance(app.screen, ApprovalScreen) and app.screen.event.tool_use_id == "b3",
        )
        await pilot.press("r")
        await pilot.pause()
        app.screen.query_one("#reason", Input).value = "keep build/, it's cached"
        await pilot.press("enter")
        await until(pilot, lambda: app.view is not None and app.view.finished)
        text = log_text(app)
        assert "always allowing here: bash: python -m pytest" in text
        assert "blocked by builtin:protected-path" in text
        assert app.view is not None and app.view.blocked == 1
    last = rig.providers[0].requests[-1].messages
    results = {
        b["tool_use_id"]: b["content"]
        for m in last
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result"
    }
    assert "keep build/, it's cached" in results["b3"]
    assert results["b4"].startswith("Blocked by policy")
