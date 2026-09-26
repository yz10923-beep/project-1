"""JSON-RPC 2.0 server over NDJSON/TCP.

Each connection is served by its own task. Requests on one connection are handled in
order, so responses come back in request order. Handlers get the `Connection`, so a
long-lived command (run.subscribe) can push notifications on it after responding;
all writes to a connection go through one lock, so pushes and responses never
interleave mid-line.

Auth: when a token is set, the first request on a connection must be core.hello with
that token. 127.0.0.1 is not a security boundary: any local process (and a browser,
via DNS rebinding) can reach it, and this server runs shell commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.commands import HELLO, HelloParams, HelloResult
from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    UNAUTHORIZED,
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from kama_claude.core.transport.framing import (
    MAX_FRAME_BYTES,
    FrameTooLarge,
    read_frame,
    write_frame,
)

logger = logging.getLogger(__name__)


class Connection:
    """One client connection: serialized writes plus tasks that live as long as it does."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.authenticated = False
        self.client = "?"
        self.peer = writer.get_extra_info("peername")

    async def send(self, msg: BaseModel) -> None:
        async with self._lock:
            await write_frame(self._writer, msg)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send(JsonRpcNotification(method=method, params=params))

    def spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run `coro` until it finishes or the connection closes, whichever is first."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_crash)
        return task

    async def close_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _log_crash(task: asyncio.Task[None]) -> None:
    """A background task must never fail silently: that turns a bug into a hang."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("connection task crashed", exc_info=exc)


type Handler[P: BaseModel] = Callable[[P, Connection], Awaitable[BaseModel]]


class _Route:
    def __init__(self, params_model: type[BaseModel], handler: Handler[Any]) -> None:
        self.params_model = params_model
        self.handler = handler


class JsonRpcServer:
    def __init__(self, host: str, port: int, *, token: str | None = None) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._routes: dict[str, _Route] = {}
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self.register(HELLO, HelloParams, self._hello)

    def register[P: BaseModel](
        self, method: str, params_model: type[P], handler: Handler[P]
    ) -> None:
        """Route `method` to `handler`; params are validated against `params_model` first."""
        if method in self._routes:
            raise ValueError(f"method already registered: {method}")
        self._routes[method] = _Route(params_model, handler)

    async def start(self) -> tuple[str, int]:
        """Bind and start accepting. Raises OSError (EADDRINUSE) if the port is taken."""
        self._server = await asyncio.start_server(
            self._on_connect, host=self._host, port=self._port, limit=MAX_FRAME_BYTES
        )
        host, port = self._server.sockets[0].getsockname()[:2]
        return host, port

    async def stop(self) -> None:
        """Stop accepting, then cancel in-flight connections."""
        if self._server is None:
            return
        self._server.close()
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        await self._server.wait_closed()
        self._server = None

    async def _hello(self, params: HelloParams, conn: Connection) -> HelloResult:
        if self._token is not None and not secrets.compare_digest(params.token, self._token):
            raise _Unauthorized("invalid token")
        conn.authenticated = True
        conn.client = params.client
        return HelloResult()

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        conn = Connection(writer)
        logger.debug("client connected: %s", conn.peer)
        try:
            await self._serve(reader, conn)
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("client dropped: %s", conn.peer)
        finally:
            self._connections.discard(task)
            await conn.close_tasks()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (TimeoutError, ConnectionError):
                pass
            logger.debug("client disconnected: %s", conn.peer)

    async def _serve(self, reader: asyncio.StreamReader, conn: Connection) -> None:
        while True:
            try:
                line = await read_frame(reader)
            except FrameTooLarge:
                # Framing is lost after an overrun, so reply once and drop the connection.
                await conn.send(make_error(None, INVALID_REQUEST, "Frame too large"))
                return
            if line is None:
                return
            if not line.strip():
                continue
            await conn.send(await self._dispatch(line, conn))

    async def _dispatch(self, line: bytes, conn: Connection) -> JsonRpcSuccess | JsonRpcError:
        """Turn one request line into exactly one response. Never raises."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            return make_error(None, PARSE_ERROR, "Parse error", str(e))

        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError as e:
            rid = raw.get("id") if isinstance(raw, dict) else None
            rid = rid if isinstance(rid, str | int) and not isinstance(rid, bool) else None
            return make_error(rid, INVALID_REQUEST, "Invalid Request", e.errors(include_url=False))

        if self._token is not None and not conn.authenticated and req.method != HELLO:
            return make_error(req.id, UNAUTHORIZED, f"Unauthorized: send {HELLO} first")

        route = self._routes.get(req.method)
        if route is None:
            return make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}")

        try:
            params = route.params_model.model_validate(req.params)
        except ValidationError as e:
            return make_error(req.id, INVALID_PARAMS, "Invalid params", e.errors(include_url=False))

        try:
            result = await route.handler(params, conn)
        except _Unauthorized as e:
            return make_error(req.id, UNAUTHORIZED, f"Unauthorized: {e}")
        except RequestError as e:
            return make_error(req.id, INVALID_PARAMS, str(e))
        except Exception:
            # Never leak internals to the client; the traceback goes to the log.
            logger.exception("handler for %s failed", req.method)
            return make_error(req.id, INTERNAL_ERROR, "Internal error")

        return JsonRpcSuccess(id=req.id, result=result.model_dump(mode="json"))


class RequestError(Exception):
    """Raised by handlers for a request that is well-formed but can't be served
    (unknown run id, workspace is not a directory). The message goes to the client."""


class _Unauthorized(Exception):
    pass
