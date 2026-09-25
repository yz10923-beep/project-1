"""Async JSON-RPC client for talking to kama-core."""

from __future__ import annotations

import asyncio
import itertools
import json
from types import TracebackType
from typing import Self

from pydantic import BaseModel

from kama_claude.core.bus.envelope import JsonRpcError, JsonRpcRequest, JsonRpcSuccess
from kama_claude.core.transport.framing import MAX_FRAME_BYTES, read_frame, write_frame


class CoreUnavailable(Exception):
    """The daemon is not reachable (not running, wrong port, or dropped the connection)."""


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class JsonRpcClient:
    def __init__(self, host: str, port: int, *, timeout: float = 5.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

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

    async def close(self) -> None:
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

    async def call[R: BaseModel](self, method: str, params: BaseModel, result_model: type[R]) -> R:
        """Send one request and wait for its response. Not safe for concurrent use."""
        if self._reader is None or self._writer is None:
            raise RuntimeError("client is not connected")
        req = JsonRpcRequest(
            id=next(self._ids), method=method, params=params.model_dump(mode="json")
        )
        try:
            await write_frame(self._writer, req)
            line = await asyncio.wait_for(read_frame(self._reader), timeout=self._timeout)
        except (ConnectionError, TimeoutError) as e:
            raise CoreUnavailable(f"lost connection to kama-core ({e!r})") from e
        if line is None:
            raise CoreUnavailable("kama-core closed the connection")

        raw = json.loads(line)
        if "error" in raw:
            err = JsonRpcError.model_validate(raw).error
            raise RpcError(err.code, err.message, err.data)
        resp = JsonRpcSuccess.model_validate(raw)
        if resp.id != req.id:
            raise RpcError(-32603, f"response id {resp.id!r} does not match request {req.id!r}")
        return result_model.model_validate(resp.result)
