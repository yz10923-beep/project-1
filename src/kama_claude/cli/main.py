"""`kama` CLI: a thin client of kama-core. Runs execute in the daemon; the CLI starts
them, watches their event stream and answers approval prompts. `--local` runs the agent
in this process instead (no daemon), as in S1."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from kama_claude import __version__
from kama_claude.core.agent.loop import ApprovalDecision, Approver
from kama_claude.core.agent.runner import TRACE_FILE, load_policy, run_goal, runs_root, sandbox_for
from kama_claude.core.agent.sinks import ConsolePrinter
from kama_claude.core.bus.commands import (
    APPROVAL_RESPOND,
    EVENT_NOTIFICATION,
    NOTES_ADD,
    NOTES_DELETE,
    NOTES_LIST,
    NOTES_UPDATE,
    PING,
    PLAN_EDIT,
    PLAN_GET,
    RUN_CANCEL,
    RUN_LIST,
    RUN_START,
    RUN_SUBSCRIBE,
    SESSION_CREATE,
    SESSION_GET,
    SESSION_LIST,
    STREAM_END_NOTIFICATION,
    ApprovalRespondParams,
    ApprovalRespondResult,
    NoteResult,
    NotesAddParams,
    NotesDeleteParams,
    NotesListParams,
    NotesListResult,
    NotesUpdateParams,
    PingParams,
    PlanEditParams,
    PlanEditResult,
    PlanGetParams,
    PlanGetResult,
    PongResult,
    RunCancelParams,
    RunCancelResult,
    RunListParams,
    RunListResult,
    RunStartParams,
    RunStartResult,
    RunSubscribeParams,
    RunSubscribeResult,
    SessionCreateParams,
    SessionGetParams,
    SessionListParams,
    SessionListResult,
    SessionView,
    StreamEnd,
)
from kama_claude.core.bus.events import (
    EVENT_ADAPTER,
    RunFinishedEvent,
    ToolApprovalRequestedEvent,
    is_durable,
)
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.llm.types import ToolCall
from kama_claude.core.notes import Note, render_note
from kama_claude.core.plan import NewTask, TaskChange, render_tasks
from kama_claude.core.policy.engine import MODES, PolicyFileError, denial_message
from kama_claude.core.session import SessionStore
from kama_claude.core.trace.analyze import load_spans, render, to_chrome
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError, read_token

EXIT_OK = 0
EXIT_FAILED = 1  # rpc error, or an agent run that did not complete
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130

# Decides an approval request: True/False to answer, None to leave it to another client.
# None = leave it to another client; a bool is a plain yes/no.
type Answerer = Callable[[ToolApprovalRequestedEvent], Awaitable[ApprovalDecision | bool | None]]


def connect(settings: Settings) -> JsonRpcClient:
    token = read_token(settings.token_file)
    if token is None:
        raise CoreUnavailable(f"no token at {settings.token_file}; is kama-core running?")
    return JsonRpcClient(settings.host, settings.port, token=token)


def _describe(name: str, tool_input: dict[str, object]) -> str:
    if name == "bash":
        return f"bash: {tool_input.get('command')}"
    if name == "write_file":
        size = len(str(tool_input.get("content", "")))
        return f"write_file: {tool_input.get('path')} ({size} chars)"
    return f"{name}: {json.dumps(tool_input)[:300]}"


APPROVAL_HELP = "y = yes · a = always (this session) · n = no · n <why> = no, and tell the model"


def parse_answer(raw: str) -> ApprovalDecision:
    """`y`, `a` (always: remember for the session), `n`, or `n <reason>` (passed on to the
    model so it can adjust). Anything else is a no."""
    text = raw.strip()
    word, _, rest = text.partition(" ")
    word = word.lower().rstrip(",:")
    if word in {"y", "yes"}:
        return ApprovalDecision(True, "user")
    if word in {"a", "always"}:
        return ApprovalDecision(True, "user", remember=True)
    if word in {"n", "no"}:
        return ApprovalDecision(False, "user", reason=rest.strip())
    return ApprovalDecision(False, "user", reason=text if len(text) > 3 else "")


async def _ask_user(
    name: str, tool_input: dict[str, object], can_remember: bool = False
) -> ApprovalDecision:
    choices = "[y/a/N]" if can_remember else "[y/N]"
    answer = await asyncio.to_thread(input, f"  ? allow {_describe(name, tool_input)} {choices} ")
    decision = parse_answer(answer)
    if decision.remember and not can_remember:
        return ApprovalDecision(True, "user")
    return decision


def make_answerer(auto_yes: bool, *, deny_if_not_tty: bool) -> Answerer | None:
    """Interactive prompt on a TTY. Without a TTY: deny (kama run) or stay silent and let
    another client answer (kama attach)."""
    if auto_yes:
        return None  # the daemon auto-approves; there is nothing to answer
    if sys.stdin.isatty():

        async def ask(event: ToolApprovalRequestedEvent) -> ApprovalDecision | None:
            return await _ask_user(event.name, event.input, bool(event.remember))

        return ask
    if deny_if_not_tty:

        async def deny(event: ToolApprovalRequestedEvent) -> ApprovalDecision | None:
            print(f"  ! denied (non-interactive; pass --yes): {_describe(event.name, event.input)}")
            return ApprovalDecision(False, "user", reason="no one was there to approve it")

        return deny
    return None


async def watch(
    client: JsonRpcClient, run_id: str, from_seq: int, answer: Answerer | None
) -> RunFinishedEvent | None:
    """Render a run's events until it finishes. Re-subscribes transparently if the daemon
    cut this client off for falling behind (reason=lagged)."""
    printer = ConsolePrinter(sys.stdout)
    finished: RunFinishedEvent | None = None
    next_seq = from_seq
    prompts: set[asyncio.Task[None]] = set()

    async def respond(event: ToolApprovalRequestedEvent) -> None:
        assert answer is not None
        decision = await answer(event)
        if decision is None:
            return
        if isinstance(decision, bool):
            decision = ApprovalDecision(decision, "user")
        res = await client.call(
            APPROVAL_RESPOND,
            ApprovalRespondParams(
                run_id=run_id,
                tool_use_id=event.tool_use_id,
                approve=decision.approved,
                reason=decision.reason,
                remember=decision.remember,
            ),
            ApprovalRespondResult,
        )
        if not res.accepted:
            print("  (already answered elsewhere or expired)")

    try:
        while True:
            await client.call(
                RUN_SUBSCRIBE,
                RunSubscribeParams(run_id=run_id, from_seq=next_seq),
                RunSubscribeResult,
            )
            async for note in client.notifications():
                if note.method == EVENT_NOTIFICATION:
                    event = EVENT_ADAPTER.validate_python(note.params["event"])
                    await printer.emit(event)
                    if is_durable(event):
                        next_seq = event.seq + 1  # type: ignore[union-attr]
                    if isinstance(event, ToolApprovalRequestedEvent) and answer is not None:
                        task = asyncio.create_task(respond(event))
                        prompts.add(task)
                        task.add_done_callback(prompts.discard)
                    if isinstance(event, RunFinishedEvent):
                        finished = event
                elif note.method == STREAM_END_NOTIFICATION:
                    end = StreamEnd.model_validate(note.params)
                    if end.reason == "lagged":
                        next_seq = end.next_seq
                        break  # re-subscribe from where we are
                    return finished
            else:
                raise CoreUnavailable("kama-core closed the connection")
    finally:
        for task in prompts:
            task.cancel()


async def _ping(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        t0 = time.perf_counter()
        pong = await client.call(PING, PingParams(client="kama-cli"), PongResult)
        latency_ms = (time.perf_counter() - t0) * 1000
    print(f"pong server={pong.server_version} uptime={pong.uptime_ms}ms latency={latency_ms:.1f}ms")
    if pong.policy is not None:
        print(f"policy {'on' if pong.policy else 'OFF (KAMA_POLICY=false)'}")
    if pong.sandbox is not None:
        print(f"sandbox {pong.sandbox}")
    return EXIT_OK


def _exit_code(finished: RunFinishedEvent | None) -> int:
    return EXIT_OK if finished is not None and finished.status == "completed" else EXIT_FAILED


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    if args.local:
        return await _run_local(settings, args)
    async with connect(settings) as client:
        started = await client.call(
            RUN_START,
            RunStartParams(
                goal=args.goal,
                workspace=str(args.workspace),
                model=args.model,
                max_steps=args.max_steps,
                auto_approve=args.yes,
                session_id=args.session,
                new_session=args.new_session,
                mode=args.mode,
            ),
            RunStartResult,
        )
        if args.detach:
            print(started.run_id)
            return EXIT_OK
        if args.new_session:
            print(f"session {started.session_id} (continue it with --session)")
        answer = make_answerer(args.yes, deny_if_not_tty=True)
        try:
            finished = await watch(client, started.run_id, 0, answer)
        except asyncio.CancelledError:
            # Ctrl+C on `kama run` means stop the run. (Closing the terminal does not:
            # the run carries on and `kama attach` picks it up.)
            with contextlib.suppress(Exception):
                await client.call(
                    RUN_CANCEL, RunCancelParams(run_id=started.run_id), RunCancelResult
                )
                print(f"\ncancelled run {started.run_id}", file=sys.stderr)
            raise
        print(f"\nevents: {started.run_dir}/events.jsonl")
        return _exit_code(finished)


async def _attach(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        answer = make_answerer(False, deny_if_not_tty=False)
        finished = await watch(client, args.run_id, args.from_seq, answer)
        return _exit_code(finished)


async def _runs(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        res = await client.call(RUN_LIST, RunListParams(), RunListResult)
    if not res.runs:
        print("no runs since kama-core started")
    for r in res.runs:
        waiting = f" · {r.pending_approvals} awaiting approval" if r.pending_approvals else ""
        plan = f" · plan {r.plan_done}/{r.plan_total}" if r.plan_total else ""
        print(f"{r.run_id}  {r.status:<10} {r.goal[:60]!r}{plan}{waiting}")
    return EXIT_OK


async def _cancel(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        res = await client.call(RUN_CANCEL, RunCancelParams(run_id=args.run_id), RunCancelResult)
    print("cancelled" if res.cancelled else "not running")
    return EXIT_OK if res.cancelled else EXIT_FAILED


async def _plan(settings: Settings, args: argparse.Namespace) -> int:
    """Show a run's plan, or steer a live one (the model is told at its next call)."""
    async with connect(settings) as client:
        if args.plan_command == "show":
            got = await client.call(PLAN_GET, PlanGetParams(run_id=args.run_id), PlanGetResult)
            print(render_tasks(got.tasks) + ("" if got.live else "\n(run finished)"))
            return EXIT_OK
        if args.plan_command == "add":
            params = PlanEditParams(
                run_id=args.run_id,
                add=[
                    NewTask(title=args.title, description=args.description, blocked_by=args.after)
                ],
            )
        else:  # cancel
            params = PlanEditParams(
                run_id=args.run_id,
                changes=[TaskChange(id=args.task_id, status="cancelled", note=args.reason)],
            )
        res = await client.call(PLAN_EDIT, params, PlanEditResult)
    print(f"{res.summary}\n{render_tasks(res.tasks)}")
    return EXIT_OK


def _tui(settings: Settings, args: argparse.Namespace) -> int:
    from kama_claude.tui.app import KamaTui  # Textual loads only for this command

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"kama: workspace is not a directory: {workspace}", file=sys.stderr)
        return EXIT_USAGE
    root = runs_root(settings, Path.cwd())

    def trace_report(run_id: str) -> str | None:
        path = root / run_id / TRACE_FILE
        if not path.is_file():
            return None
        try:
            return render(load_spans(path), width=60)
        except ValueError:  # no run span yet: the run is still starting
            return None

    KamaTui(
        lambda: connect(settings),
        workspace,
        run_id=args.run_id,
        goal=args.goal,
        auto_approve=args.yes,
        trace_report=trace_report,
        session_id=args.session,
    ).run()
    return EXIT_OK


class UsageError(Exception):
    """Bad arguments (exit 2), as opposed to the daemon being unreachable (exit 3)."""


def _workspace(raw: str) -> Path:
    ws = Path(raw).resolve()
    if not ws.is_dir():
        raise UsageError(f"workspace is not a directory: {ws}")
    return ws


async def _chat(settings: Settings, args: argparse.Namespace) -> int:
    """A conversation: every line is a run in one session, so each run sees the ones
    before it. /notes, /note TEXT, /new, /quit. Ctrl+C stops the current run and leaves;
    `kama chat --session ID` picks the conversation up again."""
    ws = _workspace(args.workspace)
    async with connect(settings) as client:
        if args.session:
            view = await client.call(
                SESSION_GET, SessionGetParams(session_id=args.session), SessionView
            )
            session_id = view.session_id
            print(f"session {session_id} · continuing after {len(view.runs)} run(s)")
        else:
            session_id = (
                await client.call(
                    SESSION_CREATE, SessionCreateParams(workspace=str(ws)), SessionView
                )
            ).session_id
            print(f"session {session_id} · new, in {ws}")
        print("type a goal · /notes · /note TEXT · /new (fresh conversation) · /quit")
        answer = make_answerer(args.yes, deny_if_not_tty=True)
        while True:
            try:
                line = (await asyncio.to_thread(input, "\nyou> ")).strip()
            except EOFError:
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/new":
                session_id = (
                    await client.call(
                        SESSION_CREATE, SessionCreateParams(workspace=str(ws)), SessionView
                    )
                ).session_id
                print(f"session {session_id} · new (notes in this workspace carry over)")
                continue
            if line == "/notes":
                got = await client.call(
                    NOTES_LIST,
                    NotesListParams(workspace=str(ws), session_id=session_id),
                    NotesListResult,
                )
                _print_notes(got.notes)
                continue
            if line.startswith("/note "):
                res = await client.call(
                    NOTES_ADD, NotesAddParams(workspace=str(ws), text=line[6:]), NoteResult
                )
                print(f"saved [{res.note.id}]; runs from now on will see it")
                continue
            started = await client.call(
                RUN_START,
                RunStartParams(
                    goal=line,
                    workspace=str(ws),
                    auto_approve=args.yes,
                    session_id=session_id,
                    mode=args.mode,
                ),
                RunStartResult,
            )
            try:
                await watch(client, started.run_id, 0, answer)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await client.call(
                        RUN_CANCEL, RunCancelParams(run_id=started.run_id), RunCancelResult
                    )
                print(f"\ncancelled run {started.run_id}; resume with --session {session_id}")
                raise
    print(f"\nsession {session_id}: `kama chat --session {session_id}` to continue")
    return EXIT_OK


def _print_notes(notes: list[Note]) -> None:
    if not notes:
        print("no notes")
        return
    now = datetime.now(UTC)
    for n in notes:
        print(render_note(n, now))


async def _session(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        if args.session_command == "list":
            ws = None if args.all else str(_workspace(args.workspace))
            res = await client.call(
                SESSION_LIST, SessionListParams(workspace=ws), SessionListResult
            )
            if not res.sessions:
                print("no sessions" + ("" if args.all else " in this workspace (try --all)"))
            for s in res.sessions:
                live = f" · running {s.active_run_id}" if s.active_run_id else ""
                print(f"{s.session_id}  {len(s.runs):>3} run(s)  {s.title[:60]!r}{live}")
            return EXIT_OK
        view = await client.call(
            SESSION_GET, SessionGetParams(session_id=args.session_id), SessionView
        )
    print(f"session {view.session_id} · {view.workspace} · {len(view.runs)} run(s)")
    for r in view.runs:
        print(f"  {r.run_id}  {r.status:<10} {r.goal.strip().splitlines()[0][:70]!r}")
    return EXIT_OK


async def _notes(settings: Settings, args: argparse.Namespace) -> int:
    ws = str(_workspace(args.workspace))
    async with connect(settings) as client:
        cmd = args.notes_command
        if cmd == "list":
            got = await client.call(
                NOTES_LIST, NotesListParams(workspace=ws, session_id=args.session), NotesListResult
            )
            _print_notes(got.notes)
            return EXIT_OK
        if cmd == "add":
            res = await client.call(
                NOTES_ADD,
                NotesAddParams(
                    workspace=ws,
                    text=args.text,
                    scope="session" if args.session else "workspace",
                    session_id=args.session,
                    source=args.source,
                    volatile=args.volatile,
                ),
                NoteResult,
            )
        elif cmd == "edit":
            res = await client.call(
                NOTES_UPDATE,
                NotesUpdateParams(
                    workspace=ws,
                    note_id=args.note_id,
                    session_id=args.session,
                    text=args.text,
                    source=args.source,
                    volatile=args.volatile,
                ),
                NoteResult,
            )
        else:
            res = await client.call(
                NOTES_DELETE,
                NotesDeleteParams(workspace=ws, note_id=args.note_id, session_id=args.session),
                NoteResult,
            )
            print(f"deleted [{res.note.id}]")
            return EXIT_OK
    print(render_note(res.note, datetime.now(UTC)))
    return EXIT_OK


def _trace(settings: Settings, args: argparse.Namespace) -> int:
    """Read a run's trace from disk (no daemon needed) and report where the time went."""
    root = runs_root(settings, Path.cwd())
    run_id = args.run_id
    if run_id is None:  # latest run: ids sort by start time
        candidates = sorted(
            p.parent.name for p in root.glob(f"*/{TRACE_FILE}") if p.parent.name != "_daemon"
        )
        if not candidates:
            print(f"kama: no traced runs in {root}", file=sys.stderr)
            return EXIT_USAGE
        run_id = candidates[-1]
    path = root / run_id / TRACE_FILE
    if not path.is_file():
        print(f"kama: no trace at {path}", file=sys.stderr)
        return EXIT_USAGE
    spans = load_spans(path)
    if args.chrome:
        Path(args.chrome).write_text(json.dumps(to_chrome(spans)))
        print(f"wrote {args.chrome}: open it at https://ui.perfetto.dev")
        return EXIT_OK
    print(render(spans, width=args.width))
    return EXIT_OK


def make_approver(auto_yes: bool) -> Approver:
    """In-process (--local) approvals."""

    async def approve(call: ToolCall) -> ApprovalDecision:
        if auto_yes:
            return ApprovalDecision(True, "auto")
        if not sys.stdin.isatty():
            print(f"  ! denied (non-interactive; pass --yes): {_describe(call.name, call.input)}")
            return ApprovalDecision(False, "user", reason="no one was there to approve it")
        return await _ask_user(call.name, call.input, can_remember=True)

    return approve


async def _run_local(settings: Settings, args: argparse.Namespace) -> int:
    overrides = {k: v for k, v in {"model": args.model, "max_steps": args.max_steps}.items() if v}
    settings = settings.model_copy(update=overrides)
    store = SessionStore(settings.sessions_dir)
    session_id = args.session
    if args.new_session:
        session_id = store.create(args.workspace).session_id
        print(f"session {session_id} (continue it with --session)")
    result, run_dir = await run_goal(
        args.goal,
        settings=settings,
        workspace=args.workspace,
        approver=make_approver(args.yes),
        extra_sink=ConsolePrinter(sys.stdout),
        session_id=session_id,
        sessions=store,
        mode=args.mode or ("auto" if args.yes else None),
    )
    print(f"\nevents: {run_dir / 'events.jsonl'}")
    return EXIT_OK if result.status == "completed" else EXIT_FAILED


def _policy(settings: Settings, args: argparse.Namespace) -> int:
    """Show the policy for a workspace, or check what it would decide for a call. Reads the
    same files the daemon reads; no daemon needed."""
    ws = _workspace(args.workspace)
    try:
        policy = load_policy(settings, ws, mode=args.mode)
    except PolicyFileError as e:
        print(f"kama: {e}", file=sys.stderr)
        return EXIT_USAGE
    if policy is None:
        print("policy is off (KAMA_POLICY=false): bash and write_file ask, -y approves all")
        return EXIT_OK
    if args.policy_command == "show":
        info = policy.describe()
        print(f"mode {info['mode']} · workspace {ws}")
        print(f"sandbox {sandbox_for(settings).describe()}")
        print(
            f"user file {settings.policy_file.expanduser()} · workspace file {ws}/.kama/policy.toml"
        )
        for label in ("user_rules", "workspace_rules"):
            rules = info[label]
            print(f"{label.replace('_', ' ')}: {len(rules) or 'none'}")
            for r in rules:
                print(f"  {r.pop('id', '')}: {json.dumps(r)}")
        for w in info["warnings"]:
            print(f"warning: {w}")
        return EXIT_OK
    if args.tool == "bash":
        if not args.target:
            raise UsageError("give a command to check: kama policy check 'rm -rf build'")
        tool_input: dict[str, object] = {"command": args.target}
    else:
        tool_input = {"path": args.target or ".", "content": ""}
    d = policy.check(args.tool, tool_input)
    print(f"{d.action.upper()} ({d.kind}, {d.risk} risk) by {d.rule}")
    print(f"  {d.reason}")
    for effect in d.effects:
        print(f"  - {effect.kind}: {effect.what}")
    for o in d.opaque:
        print(f"  ? {o}")
    if d.network:
        print("  network: this call may use the network (the sandbox opens it)")
    if d.remember:
        print("  'always' would allow: " + ", ".join(r.command or r.tool for r in d.remember))
    if d.action == "deny":
        print(f"\nthe model would read:\n  {denial_message(d)}")
    return {"allow": EXIT_OK, "ask": EXIT_OK, "deny": EXIT_FAILED}[d.action]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kama", description="Local coding agent.")
    parser.add_argument("--version", action="version", version=f"kama {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check that kama-core is up and measure round-trip latency")
    run = sub.add_parser("run", help="run the agent on a goal (in kama-core) and watch it")
    run.add_argument("goal", help="what the agent should do, in natural language")
    run.add_argument("-y", "--yes", action="store_true", help="unattended: --mode auto")
    run.add_argument(
        "--mode",
        choices=MODES,
        help="permission mode (default: the user policy file's, else default; -y = auto)",
    )
    run.add_argument("-w", "--workspace", default=".", help="directory the agent works in")
    run.add_argument("--model", help="override KAMA_MODEL")
    run.add_argument("--max-steps", type=int, help="override KAMA_MAX_STEPS")
    run.add_argument("--detach", action="store_true", help="print the run id and exit")
    run.add_argument("--local", action="store_true", help="run in this process, no daemon")
    in_session = run.add_mutually_exclusive_group()
    in_session.add_argument("--session", metavar="ID", help="continue this session")
    in_session.add_argument("--new-session", action="store_true", help="start a session")
    chat = sub.add_parser("chat", help="a conversation: each line is a run in one session")
    chat.add_argument("-w", "--workspace", default=".", help="directory the agent works in")
    chat.add_argument("-y", "--yes", action="store_true", help="unattended: --mode auto")
    chat.add_argument(
        "--mode",
        choices=MODES,
        help="permission mode (default: the user policy file's, else default; -y = auto)",
    )
    chat.add_argument("--session", metavar="ID", help="continue this session")
    session = sub.add_parser("session", help="list sessions or show one")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    s_list = session_sub.add_parser("list", help="sessions in a workspace, newest first")
    s_list.add_argument("-w", "--workspace", default=".")
    s_list.add_argument("--all", action="store_true", help="in every workspace")
    s_show = session_sub.add_parser("show", help="a session's runs")
    s_show.add_argument("session_id")
    notes = sub.add_parser("notes", help="the agent's durable notes for a workspace")
    notes_sub = notes.add_subparsers(dest="notes_command", required=True)
    for name, help_text in (
        ("list", "notes runs here will see"),
        ("add", "add a note (yours); workspace scope unless --session"),
        ("edit", "change a note"),
        ("rm", "delete a note"),
    ):
        n = notes_sub.add_parser(name, help=help_text)
        n.add_argument("-w", "--workspace", default=".")
        n.add_argument("--session", metavar="ID", help="include / use this session's notes")
        if name in ("edit", "rm"):
            n.add_argument("note_id")
        if name == "add":
            n.add_argument("text")
        if name == "edit":
            n.add_argument("--text")
        if name in ("add", "edit"):
            n.add_argument("--source", default="" if name == "add" else None)
            vol = n.add_mutually_exclusive_group()
            vol.add_argument("--volatile", dest="volatile", action="store_true", default=None)
            vol.add_argument("--stable", dest="volatile", action="store_false")
    attach = sub.add_parser("attach", help="watch a run (and answer its approvals)")
    attach.add_argument("run_id")
    attach.add_argument("--from-seq", type=int, default=0, help="replay from this event seq")
    sub.add_parser("runs", help="list runs in kama-core")
    cancel = sub.add_parser("cancel", help="stop a run")
    cancel.add_argument("run_id")
    plan = sub.add_parser("plan", help="show a run's plan, or steer a live one")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    show = plan_sub.add_parser("show", help="print the plan")
    show.add_argument("run_id")
    add = plan_sub.add_parser("add", help="add a task to a live run's plan")
    add.add_argument("run_id")
    add.add_argument("title")
    add.add_argument("--description", default="")
    add.add_argument("--after", type=int, nargs="+", default=[], metavar="ID", help="blocked by")
    cancel_task = plan_sub.add_parser("cancel", help="cancel a task in a live run's plan")
    cancel_task.add_argument("run_id")
    cancel_task.add_argument("task_id", type=int)
    cancel_task.add_argument("--reason", required=True, help="told to the model")
    tui = sub.add_parser("tui", help="full-screen UI: start, watch, approve and steer runs")
    tui.add_argument("run_id", nargs="?", help="watch this run (default: start from a goal)")
    tui.add_argument("-w", "--workspace", default=".", help="directory new runs work in")
    tui.add_argument("-y", "--yes", action="store_true", help="auto-approve new runs")
    tui.add_argument("--goal", help="start a run with this goal right away")
    tui.add_argument("--session", metavar="ID", help="continue this conversation")
    pol = sub.add_parser("policy", help="the permission policy: show it, or check a call")
    pol_sub = pol.add_subparsers(dest="policy_command", required=True)
    for name, help_text in (("show", "mode, sandbox and rules"), ("check", "what it decides")):
        pp = pol_sub.add_parser(name, help=help_text)
        pp.add_argument("-w", "--workspace", default=".")
        pp.add_argument("--mode", choices=MODES)
        if name == "check":
            pp.add_argument("target", nargs="?", help="a bash command, or a path for --tool")
            pp.add_argument("--tool", default="bash", choices=["bash", "write_file", "read_file"])
    trace = sub.add_parser("trace", help="where a run's time and tokens went")
    trace.add_argument("run_id", nargs="?", help="default: the latest run")
    trace.add_argument("--chrome", metavar="FILE", help="write Chrome trace JSON (Perfetto)")
    trace.add_argument("--width", type=int, default=40, help="timeline width in characters")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"kama: invalid configuration:\n{e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e
    if args.command == "run":
        args.workspace = Path(args.workspace).resolve()
        if not args.workspace.is_dir():
            print(f"kama: workspace is not a directory: {args.workspace}", file=sys.stderr)
            raise SystemExit(EXIT_USAGE)

    commands = {
        "ping": _ping,
        "run": _run,
        "attach": _attach,
        "runs": _runs,
        "cancel": _cancel,
        "plan": _plan,
        "chat": _chat,
        "session": _session,
        "notes": _notes,
    }
    if args.command == "trace":  # reads files only; no daemon, no event loop
        raise SystemExit(_trace(settings, args))
    if args.command == "policy":  # reads policy files only
        try:
            raise SystemExit(_policy(settings, args))
        except UsageError as e:
            print(f"kama: {e}", file=sys.stderr)
            raise SystemExit(EXIT_USAGE) from e
    if args.command == "tui":  # Textual runs its own event loop
        raise SystemExit(_tui(settings, args))
    try:
        code = asyncio.run(commands[args.command](settings, args))
    except CoreUnavailable as e:
        print(f"kama: {e}\nhint: start the daemon with `uv run kama-core`", file=sys.stderr)
        code = EXIT_UNAVAILABLE
    except RpcError as e:
        print(f"kama: {e}", file=sys.stderr)
        code = EXIT_FAILED
    except UsageError as e:
        print(f"kama: {e}", file=sys.stderr)
        code = EXIT_USAGE
    except KeyboardInterrupt:
        print("\nkama: interrupted", file=sys.stderr)
        code = EXIT_INTERRUPTED
    raise SystemExit(code)


def tui_main() -> None:
    """`kama-tui [args]` is `kama tui [args]`."""
    main(["tui", *sys.argv[1:]])
