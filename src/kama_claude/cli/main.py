"""`kama` CLI. In S1 `run` executes the agent in-process; S2 moves it into kama-core."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from kama_claude import __version__
from kama_claude.core.agent.loop import Approver
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.agent.sinks import ConsolePrinter
from kama_claude.core.bus.commands import PING, PingParams, PongResult
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.llm.types import ToolCall
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError

EXIT_OK = 0
EXIT_FAILED = 1  # rpc error, or an agent run that did not complete
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_INTERRUPTED = 130


async def _ping(settings: Settings, args: argparse.Namespace) -> int:
    async with JsonRpcClient(settings.host, settings.port) as client:
        t0 = time.perf_counter()
        pong = await client.call(PING, PingParams(client="kama-cli"), PongResult)
        latency_ms = (time.perf_counter() - t0) * 1000
    print(f"pong server={pong.server_version} uptime={pong.uptime_ms}ms latency={latency_ms:.1f}ms")
    return EXIT_OK


def _describe(call: ToolCall) -> str:
    if call.name == "bash":
        return f"bash: {call.input.get('command')}"
    if call.name == "write_file":
        size = len(str(call.input.get("content", "")))
        return f"write_file: {call.input.get('path')} ({size} chars)"
    return f"{call.name}: {json.dumps(call.input)[:300]}"


def make_approver(auto_yes: bool) -> Approver:
    async def approve(call: ToolCall) -> bool:
        if auto_yes:
            return True
        if not sys.stdin.isatty():
            print(f"  ! denied (non-interactive; pass --yes): {_describe(call)}", file=sys.stderr)
            return False
        answer = await asyncio.to_thread(input, f"  ? allow {_describe(call)} [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    return approve


async def _run(settings: Settings, args: argparse.Namespace) -> int:
    overrides = {k: v for k, v in {"model": args.model, "max_steps": args.max_steps}.items() if v}
    settings = settings.model_copy(update=overrides)
    workspace: Path = args.workspace
    result, run_dir = await run_goal(
        args.goal,
        settings=settings,
        workspace=workspace,
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
    run = sub.add_parser("run", help="run the agent on a goal until it finishes")
    run.add_argument("goal", help="what the agent should do, in natural language")
    run.add_argument("-y", "--yes", action="store_true", help="approve bash/write_file calls")
    run.add_argument("-w", "--workspace", default=".", help="directory the agent works in")
    run.add_argument("--model", help="override KAMA_MODEL")
    run.add_argument("--max-steps", type=int, help="override KAMA_MAX_STEPS")
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
    commands = {"ping": _ping, "run": _run}
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
