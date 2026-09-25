"""In-process server tests: real sockets on an ephemeral port, no subprocess."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import BaseModel

from kama_claude.core.bus.commands import PING, PingParams, PongResult
from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from kama_claude.core.config import Settings
from kama_claude.core.transport.client import CoreUnavailable, JsonRpcClient, RpcError
from kama_claude.core.transport.framing import MAX_FRAME_BYTES
from kama_claude.core.transport.server import JsonRpcServer


class Empty(BaseModel):
    pass


@pytest.fixture
async def server() -> AsyncIterator[tuple[JsonRpcServer, int]]:
    from kama_claude.core.app import CoreApp

    app = CoreApp(Settings(port=0))

    async def boom(_: Empty) -> BaseModel:
        raise RuntimeError("secret internal detail")

    app.server.register("test.boom", Empty, boom)
    _, port = await app.server.start()
    yield app.server, port
    await app.server.stop()


async def raw_exchange(port: int, payload: bytes) -> dict[str, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=MAX_FRAME_BYTES * 2)
    writer.write(payload)
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5)
    writer.close()
    await writer.wait_closed()
    result: dict[str, Any] = json.loads(line)
    return result


async def test_ping_roundtrip(server: tuple[JsonRpcServer, int]) -> None:
    _, port = server
    async with JsonRpcClient("127.0.0.1", port) as client:
        pong = await client.call(PING, PingParams(client="test"), PongResult)
    assert pong.uptime_ms >= 0


async def test_many_requests_on_one_connection_keep_order(
    server: tuple[JsonRpcServer, int],
) -> None:
    _, port = server
    async with JsonRpcClient("127.0.0.1", port) as client:
        for _ in range(20):
            await client.call(PING, PingParams(client="test"), PongResult)


async def test_concurrent_clients(server: tuple[JsonRpcServer, int]) -> None:
    _, port = server

    async def one() -> PongResult:
        async with JsonRpcClient("127.0.0.1", port) as client:
            return await client.call(PING, PingParams(client="test"), PongResult)

    results = await asyncio.gather(*(one() for _ in range(25)))
    assert len(results) == 25


@pytest.mark.parametrize(
    ("payload", "code", "rid"),
    [
        (b"{not json\n", PARSE_ERROR, None),
        (b"[1, 2]\n", INVALID_REQUEST, None),
        (b'{"jsonrpc": "2.0", "id": 3}\n', INVALID_REQUEST, 3),
        (b'{"jsonrpc": "2.0", "id": 4, "method": "nope"}\n', METHOD_NOT_FOUND, 4),
        (b'{"jsonrpc": "2.0", "id": 5, "method": "core.ping", "params": {}}\n', INVALID_PARAMS, 5),
        (b'{"jsonrpc": "2.0", "id": "s", "method": "test.boom"}\n', INTERNAL_ERROR, "s"),
    ],
)
async def test_error_codes(
    server: tuple[JsonRpcServer, int], payload: bytes, code: int, rid: object
) -> None:
    _, port = server
    resp = await raw_exchange(port, payload)
    assert resp["error"]["code"] == code
    assert resp["id"] == rid


async def test_internal_error_does_not_leak_exception_text(
    server: tuple[JsonRpcServer, int],
) -> None:
    _, port = server
    resp = await raw_exchange(port, b'{"jsonrpc": "2.0", "id": 1, "method": "test.boom"}\n')
    assert "secret" not in json.dumps(resp)


async def test_bad_request_does_not_kill_connection(server: tuple[JsonRpcServer, int]) -> None:
    _, port = server
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"garbage\n")
    writer.write(b'{"jsonrpc":"2.0","id":2,"method":"core.ping","params":{"client":"t"}}\n')
    await writer.drain()
    first = json.loads(await reader.readline())
    second = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    assert first["error"]["code"] == PARSE_ERROR
    assert second["id"] == 2 and "result" in second


async def test_oversized_frame_is_rejected(server: tuple[JsonRpcServer, int]) -> None:
    _, port = server
    resp = await raw_exchange(port, b"x" * (MAX_FRAME_BYTES + 10) + b"\n")
    assert resp["error"]["code"] == INVALID_REQUEST


async def test_rpc_error_surfaces_to_client(server: tuple[JsonRpcServer, int]) -> None:
    _, port = server
    async with JsonRpcClient("127.0.0.1", port) as client:
        with pytest.raises(RpcError) as exc:
            await client.call("nope", Empty(), PongResult)
    assert exc.value.code == METHOD_NOT_FOUND


async def test_client_reports_unavailable_when_nothing_listens() -> None:
    probe = JsonRpcServer("127.0.0.1", 0)
    _, port = await probe.start()
    await probe.stop()  # port is now closed
    with pytest.raises(CoreUnavailable):
        async with JsonRpcClient("127.0.0.1", port, timeout=1):
            pass


async def test_duplicate_registration_is_rejected() -> None:
    srv = JsonRpcServer("127.0.0.1", 0)

    async def h(_: Empty) -> Empty:
        return Empty()

    srv.register("x", Empty, h)
    with pytest.raises(ValueError):
        srv.register("x", Empty, h)
