"""JSON-RPC 2.0 server over NDJSON/TCP.

Each connection is served by its own task; requests on one connection are
handled sequentially, so responses come back in request order.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JsonRpcError,
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

type Handler[P: BaseModel] = Callable[[P], Awaitable[BaseModel]]


class _Route:
    def __init__(self, params_model: type[BaseModel], handler: Handler[Any]) -> None:
        self.params_model = params_model
        self.handler = handler


class JsonRpcServer:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._routes: dict[str, _Route] = {}
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

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

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        peer = writer.get_extra_info("peername")
        logger.debug("client connected: %s", peer)
        try:
            await self._serve(reader, writer)
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("client dropped: %s", peer)
        finally:
            self._connections.discard(task)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (TimeoutError, ConnectionError):
                pass
            logger.debug("client disconnected: %s", peer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            try:
                line = await read_frame(reader)
            except FrameTooLarge:
                # Framing is lost after an overrun, so reply once and drop the connection.
                await write_frame(writer, make_error(None, INVALID_REQUEST, "Frame too large"))
                return
            if line is None:
                return
            if not line.strip():
                continue
            await write_frame(writer, await self._dispatch(line))

    async def _dispatch(self, line: bytes) -> JsonRpcSuccess | JsonRpcError:
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

        route = self._routes.get(req.method)
        if route is None:
            return make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}")

        try:
            params = route.params_model.model_validate(req.params)
        except ValidationError as e:
            return make_error(req.id, INVALID_PARAMS, "Invalid params", e.errors(include_url=False))

        try:
            result = await route.handler(params)
        except Exception:
            # Never leak internals to the client; the traceback goes to the log.
            logger.exception("handler for %s failed", req.method)
            return make_error(req.id, INTERNAL_ERROR, "Internal error")

        return JsonRpcSuccess(id=req.id, result=result.model_dump(mode="json"))
