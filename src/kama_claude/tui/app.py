"""kama TUI: start, watch and steer runs in a full-screen terminal UI.

A thin client like the CLI: everything goes over the same JSON-RPC protocol (run.start,
run.subscribe, approval.respond, plan.edit, run.cancel, run.list), so a run started here
can be attached from the CLI and vice versa, and closing the TUI never stops a run.

Layout: status line; the run's log (streamed text, collapsible tool calls, notes) next to
its live plan; a goal box; the key bindings. Approvals open a modal; an approval answered
elsewhere closes it. If the connection drops, the TUI reconnects and resumes from the
next event seq, so nothing is shown twice or missed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Collapsible, DataTable, Footer, Input, Label, Static

from kama_claude.core.bus.commands import (
    APPROVAL_RESPOND,
    EVENT_NOTIFICATION,
    PLAN_EDIT,
    RUN_CANCEL,
    RUN_LIST,
    RUN_START,
    RUN_SUBSCRIBE,
    STREAM_END_NOTIFICATION,
    ApprovalRespondParams,
    ApprovalRespondResult,
    PlanEditParams,
    PlanEditResult,
    RunCancelParams,
    RunCancelResult,
    RunInfo,
    RunListParams,
    RunListResult,
    RunStartParams,
    RunStartResult,
    RunSubscribeParams,
    RunSubscribeResult,
    StreamEnd,
)
from kama_claude.core.bus.events import (
    EVENT_ADAPTER,
    Event,
    LLMDeltaEvent,
    LLMResponseEvent,
    PlanNoticeEvent,
    PlanReminderEvent,
    PlanUpdatedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from kama_claude.core.plan import (
    PLAN_TOOL_NAMES,
    NewTask,
    PlanTask,
    TaskChange,
    open_blocker_ids,
)
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError
from kama_claude.tui.state import RunView

type ClientFactory = Callable[[], JsonRpcClient]

MAX_OUTPUT_CHARS = 5000  # per tool block; the full output is in events.jsonl
_STATUS_STYLE = {
    "completed": "green",
    "in_progress": "bold yellow",
    "pending": "",
    "cancelled": "dim strike",
}
_MARK = {"completed": "✔", "in_progress": "▶", "pending": "○", "cancelled": "✖"}


def _describe(name: str, tool_input: dict[str, Any]) -> str:
    if name == "bash":
        return str(tool_input.get("command", ""))
    if "path" in tool_input:
        return str(tool_input["path"])
    return json.dumps(tool_input)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_plan(tasks: list[PlanTask]) -> Text:
    """The plan panel: status mark, id, title; blocked tasks dim, user tasks tagged."""
    if not tasks:
        return Text("No plan yet.", style="dim")
    done = sum(t.status == "completed" for t in tasks)
    out = Text(f"{done}/{len(tasks)} completed\n\n", style="bold")
    for t in tasks:
        blockers = open_blocker_ids(t, tasks) if t.status == "pending" else []
        style = "dim" if blockers else _STATUS_STYLE[t.status]
        out.append(f"{_MARK[t.status]} {t.id}. {t.title}", style=style)
        if blockers:
            out.append(f"  after {', '.join(map(str, blockers))}", style="dim")
        if t.added_by == "user":
            out.append("  (you)", style="cyan")
        if t.note:
            out.append(f"\n     {t.note}", style="italic dim")
        out.append("\n")
    return out


# ---------------------------------------------------------------- log widgets


class TextBlock(Static):
    """The model's text for one step, growing as deltas stream in."""

    def __init__(self) -> None:
        super().__init__("", classes="text")
        self.text = ""

    def append(self, chunk: str) -> None:
        self.text += chunk
        self.update(Text(self.text))


class ToolBlock(Collapsible):
    """One tool call: a one-line summary that expands to the input and the output."""

    def __init__(self, event: ToolStartedEvent) -> None:
        self.tool_name = event.name
        self.detail = Static(Text(json.dumps(event.input, indent=2)), classes="detail")
        super().__init__(
            self.detail,
            title=f"→ {event.name}  {_clip(_describe(event.name, event.input), 70)}",
            collapsed=True,
            classes="tool",
        )
        self._input = event.input

    def finish(self, event: ToolFinishedEvent) -> None:
        mark = "denied" if event.denied else ("error" if event.is_error else "ok")
        wait = f", waited {event.approval_ms / 1000:.1f}s" if event.approval_ms else ""
        self.title = (
            f"{'✖' if event.is_error else '✔'} {self.tool_name}  "
            f"{_clip(_describe(self.tool_name, self._input), 50)}  "
            f"[{mark} · {event.duration_ms}ms{wait}]"
        )
        output = event.output
        if len(output) > MAX_OUTPUT_CHARS:
            output = output[:MAX_OUTPUT_CHARS] + f"\n… ({len(event.output):,} chars in total)"
        body = Text(json.dumps(self._input, indent=2) + "\n\n", style="dim")
        body.append(output, style="red" if event.is_error else "")
        self.detail.update(body)
        if event.is_error:
            self.add_class("failed")


# ---------------------------------------------------------------- modals


class ApprovalScreen(ModalScreen[bool | None]):
    """Allow or deny one tool call. Escape leaves it to another client (or the timeout)."""

    BINDINGS = [
        Binding("y", "answer(True)", "Allow"),
        Binding("n", "answer(False)", "Deny"),
        Binding("escape", "answer(None)", "Leave it"),
    ]

    def __init__(self, event: ToolApprovalRequestedEvent) -> None:
        super().__init__()
        self.event = event

    def compose(self) -> ComposeResult:
        e = self.event
        body = e.input.get("command") if e.name == "bash" else None
        if e.name == "write_file":
            body = f"{e.input.get('path')}\n\n{str(e.input.get('content', ''))[:2000]}"
        with Vertical(id="dialog"):
            yield Label(f"Allow {e.name}? (step {e.step})", id="question")
            yield Static(Text(str(body if body is not None else json.dumps(e.input, indent=2))))
            yield Label("y allow · n deny · esc leave it to another client", classes="hint")

    def action_answer(self, answer: bool | None) -> None:
        self.dismiss(answer)


class ChoiceScreen(ModalScreen[str | None]):
    """What to do with text typed into the goal box while a run is live: typing there
    used to start a second run when the user meant to steer the current one."""

    BINDINGS = [
        Binding("t", "choose('task')", "Add as task"),
        Binding("n", "choose('new')", "New run"),
        Binding("escape", "choose(None)", "Cancel"),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("A run is in progress. What should this do?", id="question")
            yield Static(Text(self.text))
            yield Label(
                "t add it as a task to the current run · n start a new run · esc cancel",
                classes="hint",
            )

    def action_choose(self, choice: str | None) -> None:
        self.dismiss(choice)


class FormScreen(ModalScreen[dict[str, str] | None]):
    """A few labelled inputs; Enter on the last one submits, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, fields: list[tuple[str, str]]) -> None:
        super().__init__()
        self.form_title = title
        self.fields = fields  # (name, label)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.form_title, id="question")
            for name, label in self.fields:
                yield Label(label, classes="hint")
                yield Input(id=f"f-{name}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        if event.input is not inputs[-1]:
            self.focus_next()
            return
        self.dismiss({name: self.query_one(f"#f-{name}", Input).value for name, _ in self.fields})

    def action_cancel(self) -> None:
        self.dismiss(None)


class TraceScreen(ModalScreen[None]):
    """The `kama trace` report for the run being watched."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+g", "close", "Close")]

    def __init__(self, report: str) -> None:
        super().__init__()
        self.report = report

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dialog"):
            yield Label("Trace (esc to close)", id="question")
            yield Static(Text(self.report), id="report")

    def action_close(self) -> None:
        self.dismiss(None)


class RunsScreen(ModalScreen[str | None]):
    """Pick a run to watch (runs kama-core has in memory)."""

    BINDINGS = [Binding("escape", "cancel", "Close")]

    def __init__(self, runs: list[RunInfo]) -> None:
        super().__init__()
        self.runs = runs

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Runs (Enter to watch)", id="question")
            table: DataTable[str] = DataTable(cursor_type="row", id="runs")
            table.add_columns("run", "status", "plan", "goal")
            # started_at, not run_id: ids have 1s resolution plus a random suffix
            for r in sorted(self.runs, key=lambda r: r.started_at, reverse=True):
                plan = f"{r.plan_done}/{r.plan_total}" if r.plan_total else ""
                table.add_row(r.run_id, r.status, plan, _clip(r.goal, 50), key=r.run_id)
            yield table

    def on_mount(self) -> None:
        self.query_one("#runs", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(event.row_key.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- the app


class KamaTui(App[None]):
    TITLE = "kama"
    CSS = """
    #status { height: 1; background: $primary; color: $text; padding: 0 1; }
    #body { height: 1fr; }
    #log { width: 2fr; padding: 0 1; }
    #side { width: 1fr; min-width: 32; border-left: solid $primary; padding: 0 1; }
    #side-title { text-style: bold; margin-bottom: 1; }
    #goal { margin: 0 1; }
    .runhead { color: $accent; margin-top: 1; }
    .step { color: $text-muted; margin-top: 1; }
    .text { margin-left: 2; }
    .note { color: $warning; margin-left: 2; }
    .user { color: $accent; margin-left: 2; }
    .done { color: $success; margin: 1 0; }
    .fail { color: $error; margin: 1 0; }
    Collapsible.tool { border: none; padding: 0; margin: 0 0 0 2; background: $surface; }
    Collapsible.tool.failed CollapsibleTitle { color: $error; }
    .detail { padding: 0 2; }
    ModalScreen { align: center middle; }
    #dialog { width: 90; max-width: 95%; height: auto; max-height: 80%;
              border: thick $primary; background: $surface; padding: 1 2; }
    #question { text-style: bold; margin-bottom: 1; }
    .hint { color: $text-muted; margin-top: 1; }
    """
    BINDINGS = [
        Binding("ctrl+r", "runs", "Runs", priority=True),
        Binding("ctrl+t", "add_task", "Add task", priority=True),
        Binding("ctrl+o", "drop_task", "Cancel task", priority=True),
        Binding("ctrl+s", "stop_run", "Stop run", priority=True),
        Binding("ctrl+y", "toggle_auto", "Auto-approve", priority=True),
        Binding("ctrl+g", "trace", "Trace", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        client_factory: ClientFactory,
        workspace: Path,
        *,
        run_id: str | None = None,
        goal: str | None = None,
        auto_approve: bool = False,
        reconnect_delay_s: float = 0.5,
        trace_report: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory
        self.workspace = workspace
        self.auto_approve = auto_approve
        self._initial_run = run_id
        self._initial_goal = goal
        self._reconnect_delay_s = reconnect_delay_s
        self._trace_report = trace_report  # run id -> rendered report (None: no trace yet)
        self.view: RunView | None = None
        self.connection = "idle"
        self._text: TextBlock | None = None
        self._tools: dict[str, ToolBlock] = {}
        self._approvals: dict[str, ApprovalScreen] = {}
        self._steps: dict[int, Static] = {}  # step divider, drawn when the step starts

    # -------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="body"):
            yield VerticalScroll(id="log")
            with Vertical(id="side"):
                yield Label("Plan", id="side-title")
                yield Static(render_plan([]), id="plan")
        yield Input(
            placeholder=f"Describe a goal and press Enter to run it in {self.workspace}",
            id="goal",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_status()
        self.query_one("#goal", Input).focus()
        if self._initial_goal:
            await self.start_run(self._initial_goal)
        elif self._initial_run:
            self.watch_run(self._initial_run)

    def _append(self, widget: Widget) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(widget)
        self._follow()

    def _follow(self) -> None:
        # After the next refresh: the new widget has no height until it is laid out.
        log = self.query_one("#log", VerticalScroll)
        self.call_after_refresh(log.scroll_end, animate=False)

    def _step_divider(self, step: int) -> Static:
        """The divider for `step`, drawn the first time anything of that step arrives
        (its streamed text comes before the llm.response that closes it)."""
        if step not in self._steps:
            self._break_text()
            self._steps[step] = divider = Static(Text(f"── step {step}"), classes="step")
            self._append(divider)
        return self._steps[step]

    def _note(self, text: str, cls: str = "note") -> None:
        self._append(Static(Text(text), classes=cls))

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        if self.view is None:
            auto = "auto-approve ON" if self.auto_approve else "asks before bash/write"
            status.update(Text(f"no run · {auto} · {self.connection}"))
        else:
            status.update(
                Text(self.view.headline(auto_approve=self.auto_approve, connection=self.connection))
            )

    # -------------------------------------------------------------- rpc

    def _fail(self, action: str, e: Exception) -> None:
        text = e.message if isinstance(e, RpcError) else str(e)
        if isinstance(e, CoreUnavailable):
            text += "\nstart the daemon with: uv run kama-core"
        self.notify(f"could not {action}: {text}", severity="error", timeout=8)

    async def rpc[R: BaseModel](self, method: str, params: BaseModel, result: type[R]) -> R:
        """One request on a short-lived connection (the watch keeps its own)."""
        async with self._client_factory() as client:
            return await client.call(method, params, result)

    async def start_run(self, goal: str) -> None:
        try:
            started = await self.rpc(
                RUN_START,
                RunStartParams(
                    goal=goal, workspace=str(self.workspace), auto_approve=self.auto_approve
                ),
                RunStartResult,
            )
        except (CoreUnavailable, RpcError) as e:
            self._fail("start the run", e)
            return
        self.watch_run(started.run_id)

    # -------------------------------------------------------------- watching a run

    def watch_run(self, run_id: str) -> None:
        """Show `run_id` from its first event, replacing whatever was shown."""
        self.workers.cancel_group(self, "watch")
        for screen in list(self._approvals.values()):
            self._close(screen)
        self._approvals.clear()
        self.query_one("#log", VerticalScroll).remove_children()
        self._text, self._tools, self._steps = None, {}, {}
        self.view = RunView(run_id)
        self.query_one("#plan", Static).update(render_plan([]))
        self.run_worker(self._watch(self.view), group="watch", exclusive=True)

    async def _watch(self, view: RunView) -> None:
        delay = self._reconnect_delay_s
        while True:
            try:
                async with self._client_factory() as client:
                    self._set_connection("live")
                    delay = self._reconnect_delay_s
                    if await self._stream(client, view):
                        self._set_connection("run over" if view.finished else "idle")
                        return
                raise CoreUnavailable("kama-core closed the connection")
            except RpcError as e:  # e.g. unknown run: retrying won't help
                self._note(f"cannot watch {view.run_id}: {e.message}", "fail")
                self._set_connection("error")
                return
            except CoreUnavailable:
                self._set_connection(f"disconnected, retrying in {delay:.1f}s")
                self._break_text()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    async def _stream(self, client: JsonRpcClient, view: RunView) -> bool:
        """Subscribe from view.next_seq and render until the stream ends. True when the
        run's stream is complete; False if the connection went away."""
        while True:
            await client.call(
                RUN_SUBSCRIBE,
                RunSubscribeParams(run_id=view.run_id, from_seq=view.next_seq),
                RunSubscribeResult,
            )
            async for note in client.notifications():
                if note.method == EVENT_NOTIFICATION:
                    event = EVENT_ADAPTER.validate_python(note.params["event"])
                    if view.apply(event):
                        self._render(event)
                elif note.method == STREAM_END_NOTIFICATION:
                    end = StreamEnd.model_validate(note.params)
                    if end.reason == "lagged":
                        self._note("(fell behind; catching up)")
                        break  # re-subscribe from view.next_seq
                    return True
            else:
                return False

    def _set_connection(self, state: str) -> None:
        self.connection = state
        self._refresh_status()

    def _break_text(self) -> None:
        self._text = None

    # -------------------------------------------------------------- rendering events

    def _render(self, event: Event) -> None:
        view = self.view
        assert view is not None
        if isinstance(event, LLMDeltaEvent):
            self._step_divider(event.step)
            if self._text is None:
                self._text = TextBlock()
                self._append(self._text)
            self._text.append(event.text)
            self._follow()
            return
        streamed = self._text is not None
        self._break_text()
        match event:
            case RunStartedEvent():
                self._note(f"▶ {event.run_id}  {event.goal}", "runhead")
            case LLMResponseEvent():
                u = event.usage
                self._step_divider(event.step).update(
                    Text(
                        f"── step {event.step} · {event.stop_reason} · {event.latency_ms}ms · "
                        f"in {u.input_tokens + u.cache_read_input_tokens:,} "
                        f"out {u.output_tokens:,}"
                    )
                )
                text = "".join(b.get("text", "") for b in event.content if b["type"] == "text")
                if text.strip() and not streamed:  # replayed: deltas are live-only
                    block = TextBlock()
                    self._append(block)
                    block.append(text)
            case ToolStartedEvent() if event.name not in PLAN_TOOL_NAMES:
                self._tools[event.tool_use_id] = tool_block = ToolBlock(event)
                self._append(tool_block)
            case ToolFinishedEvent():
                if (tool := self._tools.pop(event.tool_use_id, None)) is not None:
                    tool.finish(event)
                elif event.is_error:  # a rejected plan call: worth seeing
                    self._note(f"✖ {event.name}: {_clip(event.output, 200)}", "fail")
            case ToolApprovalRequestedEvent():
                # Replaying a run delivers each request with its resolution right behind
                # it; ask only if it is still pending a moment later.
                self.set_timer(0.05, lambda e=event: self._ask_if_pending(e))
            case ToolApprovalResolvedEvent():
                # Still open here = answered by another client, auto, or the timeout
                # (our own answer removes the modal before it is sent).
                if (screen := self._approvals.pop(event.tool_use_id, None)) is not None:
                    self._close(screen)
                    self.notify(f"approval answered elsewhere ({event.by})")
                if event.by not in ("user", "auto"):
                    self._note(f"approval {event.by}: {'allowed' if event.approved else 'denied'}")
            case PlanUpdatedEvent():
                self.query_one("#plan", Static).update(render_plan(event.tasks))
                if event.by == "user":
                    self._note(f"✎ plan changed: {event.summary}", "user")
            case PlanNoticeEvent():
                self._note("(the model has been told about the plan change)", "user")
            case PlanReminderEvent():
                self._note(f"! stopped with open tasks {event.open_task_ids}; reminded the model")
            case RunFinishedEvent():
                u = event.usage
                cls = "done" if event.status == "completed" else "fail"
                self._note(
                    f"■ {event.status} after {event.steps} steps · "
                    f"{event.duration_ms / 1000:.1f}s · in {u.input_tokens:,} out "
                    f"{u.output_tokens:,}" + (f"\n  {event.error}" if event.error else ""),
                    cls,
                )
            case _:
                pass
        self._refresh_status()

    # -------------------------------------------------------------- approvals

    def _close(self, screen: ApprovalScreen) -> None:
        if screen.is_current:  # Textual can only dismiss the top screen
            screen.dismiss(None)

    def _ask_if_pending(self, event: ToolApprovalRequestedEvent) -> None:
        if self.view is None or event.tool_use_id not in self.view.pending:
            return
        if event.run_id != self.view.run_id or event.tool_use_id in self._approvals:
            return
        self._ask(event)

    def _ask(self, event: ToolApprovalRequestedEvent) -> None:
        run_id = event.run_id
        screen = ApprovalScreen(event)
        self._approvals[event.tool_use_id] = screen

        async def answered(answer: bool | None) -> None:
            if self._approvals.pop(event.tool_use_id, None) is None or answer is None:
                return  # closed because it was answered elsewhere, or left to others
            try:
                res = await self.rpc(
                    APPROVAL_RESPOND,
                    ApprovalRespondParams(
                        run_id=run_id, tool_use_id=event.tool_use_id, approve=answer
                    ),
                    ApprovalRespondResult,
                )
            except (CoreUnavailable, RpcError) as e:
                self._fail("send the answer", e)
                return
            if not res.accepted:
                self.notify("already answered elsewhere, or expired")

        self.push_screen(screen, answered)

    # -------------------------------------------------------------- actions

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "goal" or not event.value.strip():
            return
        text = event.value.strip()
        event.input.value = ""
        view = self.view
        if view is None or view.finished or not view.planning:
            await self.start_run(text)
            return

        async def chosen(choice: str | None) -> None:
            if choice == "new":
                await self.start_run(text)
            elif choice == "task":
                await self._edit_plan(PlanEditParams(run_id=view.run_id, add=[NewTask(title=text)]))
            else:
                event.input.value = text  # cancelled: give the text back

        self.push_screen(ChoiceScreen(text), chosen)

    def _live_run_id(self) -> str | None:
        if self.view is None or self.view.finished:
            self.notify("no live run", severity="warning")
            return None
        return self.view.run_id

    async def _edit_plan(self, params: PlanEditParams) -> None:
        try:
            res = await self.rpc(PLAN_EDIT, params, PlanEditResult)
        except (CoreUnavailable, RpcError) as e:
            self._fail("change the plan", e)
            return
        self.notify(f"plan changed: {res.summary}")

    def action_add_task(self) -> None:
        run_id = self._live_run_id()
        if run_id is None:
            return

        async def done(values: dict[str, str] | None) -> None:
            if not values or not values["title"].strip():
                return
            try:
                after = [int(x) for x in values["after"].replace(",", " ").split()]
            except ValueError:
                self.notify("'after' takes task ids, e.g. 2 3", severity="error")
                return
            await self._edit_plan(
                PlanEditParams(
                    run_id=run_id, add=[NewTask(title=values["title"], blocked_by=after)]
                )
            )

        self.push_screen(
            FormScreen(
                "Add a task to the plan",
                [("title", "Title"), ("after", "After tasks (ids, optional)")],
            ),
            done,
        )

    def action_drop_task(self) -> None:
        run_id = self._live_run_id()
        if run_id is None:
            return

        async def done(values: dict[str, str] | None) -> None:
            if not values:
                return
            if not values["id"].strip().isdigit() or not values["reason"].strip():
                self.notify("give a task id and a reason", severity="error")
                return
            change = TaskChange(id=int(values["id"]), status="cancelled", note=values["reason"])
            await self._edit_plan(PlanEditParams(run_id=run_id, changes=[change]))

        self.push_screen(
            FormScreen(
                "Cancel a task", [("id", "Task id"), ("reason", "Reason (told to the model)")]
            ),
            done,
        )

    async def action_stop_run(self) -> None:
        run_id = self._live_run_id()
        if run_id is None:
            return
        try:
            res = await self.rpc(RUN_CANCEL, RunCancelParams(run_id=run_id), RunCancelResult)
        except (CoreUnavailable, RpcError) as e:
            self._fail("stop the run", e)
            return
        self.notify("stopping the run" if res.cancelled else "the run had already ended")

    def action_toggle_auto(self) -> None:
        self.auto_approve = not self.auto_approve
        self.notify(
            "new runs are auto-approved"
            if self.auto_approve
            else "new runs ask before bash and write_file"
        )
        self._refresh_status()

    def action_trace(self) -> None:
        if self.view is None or self._trace_report is None:
            self.notify("no run to trace", severity="warning")
            return
        report = self._trace_report(self.view.run_id)
        if report is None:
            self.notify("no trace for this run yet", severity="warning")
            return
        self.push_screen(TraceScreen(report))

    async def action_runs(self) -> None:
        try:
            res = await self.rpc(RUN_LIST, RunListParams(), RunListResult)
        except (CoreUnavailable, RpcError) as e:
            self._fail("list runs", e)
            return
        if not res.runs:
            self.notify("no runs since kama-core started")
            return

        def picked(run_id: str | None) -> None:
            if run_id:
                self.watch_run(run_id)

        self.push_screen(RunsScreen(res.runs), picked)
