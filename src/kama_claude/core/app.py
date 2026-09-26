"""kama-core daemon entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from kama_claude import __version__
from kama_claude.core.agent.manager import RunManager, UnknownRun
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
from kama_claude.core.bus.events import Event
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.llm.types import LLMProvider
from kama_claude.core.transport.server import Connection, JsonRpcServer, RequestError

logger = logging.getLogger("kama_claude.core")


def write_token(path: Path, token: str) -> None:
    """Write the auth token readable only by this user (0600, in a 0700 directory)."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)  # in case the file already existed with wider permissions


def _checked_workspace(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        raise RequestError(f"workspace must be an existing absolute directory: {raw}")
    return path.resolve()


class CoreApp:
    def __init__(
        self,
        settings: Settings,
        provider_factory: Callable[[Settings], LLMProvider] | None = None,
    ) -> None:
        self.settings = settings
        self.token = secrets.token_urlsafe(32)
        self.server = JsonRpcServer(settings.host, settings.port, token=self.token)
        self.runs = RunManager(settings, provider_factory)
        self._started = time.monotonic()
        self._stop = asyncio.Event()
        self.server.register(PING, PingParams, self.on_ping)
        self.server.register(RUN_START, RunStartParams, self.on_run_start)
        self.server.register(RUN_SUBSCRIBE, RunSubscribeParams, self.on_run_subscribe)
        self.server.register(RUN_CANCEL, RunCancelParams, self.on_run_cancel)
        self.server.register(RUN_LIST, RunListParams, self.on_run_list)
        self.server.register(APPROVAL_RESPOND, ApprovalRespondParams, self.on_approval_respond)

    async def on_ping(self, params: PingParams, conn: Connection) -> PongResult:
        logger.debug("ping from %s", params.client)
        return PongResult(
            server_version=__version__,
            uptime_ms=int((time.monotonic() - self._started) * 1000),
            received_at=datetime.now(UTC),
        )

    async def on_run_start(self, params: RunStartParams, conn: Connection) -> RunStartResult:
        workspace = await asyncio.to_thread(_checked_workspace, params.workspace)
        handle = self.runs.start(
            params.goal,
            workspace,
            auto_approve=params.auto_approve,
            model=params.model,
            max_steps=params.max_steps,
        )
        logger.info("run %s started by %s in %s", handle.run_id, conn.client, workspace)
        return RunStartResult(run_id=handle.run_id, run_dir=str(handle.run_dir))

    async def on_run_subscribe(
        self, params: RunSubscribeParams, conn: Connection
    ) -> RunSubscribeResult:
        if not self.runs.exists(params.run_id):
            raise RequestError(f"unknown run: {params.run_id}")

        async def send(event: Event) -> None:
            await conn.notify(EVENT_NOTIFICATION, {"event": event.model_dump(mode="json")})

        async def end(info: StreamEnd) -> None:
            await conn.notify(STREAM_END_NOTIFICATION, info.model_dump(mode="json"))

        async def pump() -> None:
            try:
                await self.runs.subscribe(params.run_id, params.from_seq, send, end)
            except (ConnectionError, OSError):
                pass  # client went away; the run carries on

        # Lives as long as the connection: a disconnect ends the subscription, not the run.
        conn.spawn(pump())
        return RunSubscribeResult(run_id=params.run_id, live=params.run_id in self.runs.runs)

    async def on_run_cancel(self, params: RunCancelParams, conn: Connection) -> RunCancelResult:
        try:
            return RunCancelResult(cancelled=self.runs.cancel(params.run_id))
        except UnknownRun as e:
            raise RequestError(f"unknown run: {e}") from e

    async def on_run_list(self, params: RunListParams, conn: Connection) -> RunListResult:
        return RunListResult(runs=[h.info() for h in self.runs.runs.values()])

    async def on_approval_respond(
        self, params: ApprovalRespondParams, conn: Connection
    ) -> ApprovalRespondResult:
        try:
            ok = self.runs.respond(params.run_id, params.tool_use_id, params.approve)
        except UnknownRun as e:
            raise RequestError(f"unknown run: {e}") from e
        return ApprovalRespondResult(accepted=ok)

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Install signal handlers before announcing readiness: anyone who sees the
        # "listening" line may immediately send SIGTERM and expect a clean exit.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_stop)
        host, port = await self.server.start()
        # Only after binding: a second daemon that fails to start must not overwrite the
        # running daemon's token and lock its clients out.
        write_token(self.settings.token_file, self.token)
        # Tests and scripts wait for this exact line on stderr.
        logger.info("kama-core %s listening on %s:%d", __version__, host, port)
        try:
            await self._stop.wait()
        finally:
            logger.info("kama-core shutting down")
            await self.runs.shutdown()  # each live run records run.finished (cancelled)
            await self.server.stop()


def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"kama-core: invalid configuration:\n{e}", file=sys.stderr)
        raise SystemExit(2) from e
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(CoreApp(settings).run())
    except OSError as e:
        # Most commonly EADDRINUSE: another daemon already owns the port.
        print(f"kama-core: cannot listen on {settings.host}:{settings.port}: {e}", file=sys.stderr)
        raise SystemExit(1) from e
