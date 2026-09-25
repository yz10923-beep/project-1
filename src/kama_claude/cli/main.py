"""`kama` CLI: a thin client of kama-core. All real work happens in the daemon."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from kama_claude import __version__
from kama_claude.core.bus.commands import PING, PingParams, PongResult
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError

EXIT_OK = 0
EXIT_RPC_ERROR = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3


async def _ping(settings: Settings) -> int:
    async with JsonRpcClient(settings.host, settings.port) as client:
        t0 = time.perf_counter()
        pong = await client.call(PING, PingParams(client="kama-cli"), PongResult)
        latency_ms = (time.perf_counter() - t0) * 1000
    print(f"pong server={pong.server_version} uptime={pong.uptime_ms}ms latency={latency_ms:.1f}ms")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kama", description="Client for the kama-core daemon.")
    parser.add_argument("--version", action="version", version=f"kama {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ping", help="check that kama-core is up and measure round-trip latency")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"kama: invalid configuration:\n{e}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE) from e

    commands = {"ping": _ping}
    try:
        code = asyncio.run(commands[args.command](settings))
    except CoreUnavailable as e:
        print(f"kama: {e}\nhint: start the daemon with `uv run kama-core`", file=sys.stderr)
        code = EXIT_UNAVAILABLE
    except RpcError as e:
        print(f"kama: {e}", file=sys.stderr)
        code = EXIT_RPC_ERROR
    raise SystemExit(code)
