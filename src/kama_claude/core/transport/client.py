"""Async JSON-RPC client for talking to kama-core.

A background reader routes each incoming line: responses resolve the pending call with
the same id, notifications go to a queue. So several calls can be in flight at once,
and event pushes can arrive between a request and its response.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel

from kama_claude.core.bus.commands import HELLO, HelloParams, HelloResult
from kama_claude.core.bus.envelope import (
    JsonRpcError,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcSuccess,
)
from kama_claude.core.transport.framing import MAX_FRAME_BYTES, read_frame, write_frame


class CoreUnavailable(Exception):
    """The daemon is not reachable (not running, wrong port, or dropped the connection)."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


def read_token(path: Path) -> str | None:
    try:
        return path.expanduser().read_text().strip() or None
    except OSError:
        return None


class JsonRpcClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        token: str | None = None,
        client_name: str = "kama-cli",
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._client_name = client_name
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[JsonRpcNotification | None] = asyncio.Queue()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port, limit=MAX_FRAME_BYTES),
                timeout=self._timeout,
            )
        except (OSError, TimeoutError) as e:
            raise CoreUnavailable(
                f"cannot reach kama-core at {self._host}:{self._port} ({e})"
            ) from e
        self._read_task = asyncio.create_task(self._read_loop())
        if self._token is not None:
            await self.call(
                HELLO, HelloParams(token=self._token, client=self._client_name), HelloResult
            )

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
            self._read_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except ConnectionError:
                pass
            self._writer = self._reader = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        reason = "kama-core closed the connection"
        try:
            while (line := await read_frame(self._reader)) is not None:
                raw = json.loads(line)
                if "method" in raw and "id" not in raw:
                    self._notifications.put_nowait(JsonRpcNotification.model_validate(raw))
                elif (fut := self._pending.pop(raw.get("id"), None)) is not None:
                    if not fut.done():
                        fut.set_result(raw)
        except (ConnectionError, OSError) as e:
            reason = f"lost connection to kama-core ({e!r})"
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CoreUnavailable(reason))
            self._pending.clear()
            self._notifications.put_nowait(None)

    async def call[R: BaseModel](
        self, method: str, params: BaseModel, result_model: type[R], *, wait_s: float | None = None
    ) -> R:
        """Send one request and wait for its response. Safe to use concurrently."""
        if self._writer is None or self._read_task is None or self._read_task.done():
            raise CoreUnavailable("not connected to kama-core")
        rid = next(self._ids)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        req = JsonRpcRequest(id=rid, method=method, params=params.model_dump(mode="json"))
        try:
            async with self._write_lock:
                await write_frame(self._writer, req)
            raw = await asyncio.wait_for(fut, timeout=wait_s or self._timeout)
        except (ConnectionError, TimeoutError) as e:
            raise CoreUnavailable(f"no response from kama-core ({e!r})") from e
        finally:
            self._pending.pop(rid, None)
        if "error" in raw:
            err = JsonRpcError.model_validate(raw).error
            raise RpcError(err.code, err.message, err.data)
        return result_model.model_validate(JsonRpcSuccess.model_validate(raw).result)

    async def notifications(self) -> AsyncIterator[JsonRpcNotification]:
        """Server pushes, in arrival order. Ends when the connection closes."""
        while (item := await self._notifications.get()) is not None:
            yield item
