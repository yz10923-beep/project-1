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
from pathlib import Path

from kama_claude import __version__
from kama_claude.core.agent.loop import Approver
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.agent.sinks import ConsolePrinter
from kama_claude.core.bus.commands import (
    APPROVAL_RESPOND,
    EVENT_NOTIFICATION,
    PING,
    RUN_CANCEL,
    RUN_LIST,
    RUN_START,
    RUN_SUBSCRIBE,
    STREAM_END_NOTIFICATION,
    ApprovalRespondParams,
    ApprovalRespondResult,
    PingParams,
    PongResult,
    RunCancelParams,
    RunCancelResult,
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
    RunFinishedEvent,
    ToolApprovalRequestedEvent,
    is_durable,
)
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.llm.types import ToolCall
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError, read_token

EXIT_OK = 0
EXIT_FAILED = 1  # rpc error, or an agent run that did not complete
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130

# Decides an approval request: True/False to answer, None to leave it to another client.
type Answerer = Callable[[ToolApprovalRequestedEvent], Awaitable[bool | None]]


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


async def _ask_user(name: str, tool_input: dict[str, object]) -> bool:
    answer = await asyncio.to_thread(input, f"  ? allow {_describe(name, tool_input)} [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def make_answerer(auto_yes: bool, *, deny_if_not_tty: bool) -> Answerer | None:
    """Interactive prompt on a TTY. Without a TTY: deny (kama run) or stay silent and let
    another client answer (kama attach)."""
    if auto_yes:
        return None  # the daemon auto-approves; there is nothing to answer
    if sys.stdin.isatty():

        async def ask(event: ToolApprovalRequestedEvent) -> bool | None:
            return await _ask_user(event.name, event.input)

        return ask
    if deny_if_not_tty:

        async def deny(event: ToolApprovalRequestedEvent) -> bool | None:
            print(f"  ! denied (non-interactive; pass --yes): {_describe(event.name, event.input)}")
            return False

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
        res = await client.call(
            APPROVAL_RESPOND,
            ApprovalRespondParams(run_id=run_id, tool_use_id=event.tool_use_id, approve=decision),
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
            ),
            RunStartResult,
        )
        if args.detach:
            print(started.run_id)
            return EXIT_OK
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
        print(f"{r.run_id}  {r.status:<10} {r.goal[:60]!r}{waiting}")
    return EXIT_OK


async def _cancel(settings: Settings, args: argparse.Namespace) -> int:
    async with connect(settings) as client:
        res = await client.call(RUN_CANCEL, RunCancelParams(run_id=args.run_id), RunCancelResult)
    print("cancelled" if res.cancelled else "not running")
    return EXIT_OK if res.cancelled else EXIT_FAILED


def make_approver(auto_yes: bool) -> Approver:
    """In-process (--local) approvals."""

    async def approve(call: ToolCall) -> bool:
        if auto_yes:
            return True
        if not sys.stdin.isatty():
            print(f"  ! denied (non-interactive; pass --yes): {_describe(call.name, call.input)}")
            return False
        return await _ask_user(call.name, call.input)

    return approve


async def _run_local(settings: Settings, args: argparse.Namespace) -> int:
    overrides = {k: v for k, v in {"model": args.model, "max_steps": args.max_steps}.items() if v}
    result, run_dir = await run_goal(
        args.goal,
        settings=settings.model_copy(update=overrides),
        workspace=args.workspace,
        approver=make_approver(args.yes),
        extra_sink=ConsolePrinter(sys.stdout),
    )
    print(f"\nevents: {run_dir / 'events.jsonl'}")
    return EXIT_OK if result.status == "completed" else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kama", description="Local coding agent.")
    parser.add_argument("--version", action="version", version=f"kama {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check that kama-core is up and measure round-trip latency")
    run = sub.add_parser("run", help="run the agent on a goal (in kama-core) and watch it")
    run.add_argument("goal", help="what the agent should do, in natural language")
    run.add_argument("-y", "--yes", action="store_true", help="approve bash/write_file calls")
    run.add_argument("-w", "--workspace", default=".", help="directory the agent works in")
    run.add_argument("--model", help="override KAMA_MODEL")
    run.add_argument("--max-steps", type=int, help="override KAMA_MAX_STEPS")
    run.add_argument("--detach", action="store_true", help="print the run id and exit")
    run.add_argument("--local", action="store_true", help="run in this process, no daemon")
    attach = sub.add_parser("attach", help="watch a run (and answer its approvals)")
    attach.add_argument("run_id")
    attach.add_argument("--from-seq", type=int, default=0, help="replay from this event seq")
    sub.add_parser("runs", help="list runs in kama-core")
    cancel = sub.add_parser("cancel", help="stop a run")
    cancel.add_argument("run_id")
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

    commands = {"ping": _ping, "run": _run, "attach": _attach, "runs": _runs, "cancel": _cancel}
    try:
        code = asyncio.run(commands[args.command](settings, args))
    except CoreUnavailable as e:
        print(f"kama: {e}\nhint: start the daemon with `uv run kama-core`", file=sys.stderr)
        code = EXIT_UNAVAILABLE
    except RpcError as e:
        print(f"kama: {e}", file=sys.stderr)
        code = EXIT_FAILED
    except KeyboardInterrupt:
        print("\nkama: interrupted", file=sys.stderr)
        code = EXIT_INTERRUPTED
    raise SystemExit(code)
