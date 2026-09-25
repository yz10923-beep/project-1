"""kama-core daemon entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import UTC, datetime

from kama_claude import __version__
from kama_claude.core.bus.commands import PING, PingParams, PongResult
from kama_claude.core.config import ConfigError, Settings, load_settings
from kama_claude.core.transport.server import JsonRpcServer

logger = logging.getLogger("kama_claude.core")


class CoreApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.server = JsonRpcServer(settings.host, settings.port)
        self._started = time.monotonic()
        self._stop = asyncio.Event()
        self.server.register(PING, PingParams, self.on_ping)

    async def on_ping(self, params: PingParams) -> PongResult:
        logger.debug("ping from %s", params.client)
        return PongResult(
            server_version=__version__,
            uptime_ms=int((time.monotonic() - self._started) * 1000),
            received_at=datetime.now(UTC),
        )

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Install signal handlers before announcing readiness: anyone who sees the
        # "listening" line may immediately send SIGTERM and expect a clean exit.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.request_stop)
        host, port = await self.server.start()
        # Tests and scripts wait for this exact line on stderr.
        logger.info("kama-core %s listening on %s:%d", __version__, host, port)
        try:
            await self._stop.wait()
        finally:
            logger.info("kama-core shutting down")
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
