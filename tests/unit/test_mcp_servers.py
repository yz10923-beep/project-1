"""The S7 eval servers (evals/mcp) and scripts/fake_mcp.py speak correct MCP: checked
with the official SDK's client, which kama's own client (part 2) is not, and with raw
lines and requests for the strictness kama's client will be tested against."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from evals.mcp import ledger_data
from mcp import ClientSession
from mcp.client._probe import negotiate_auto
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "evals" / "mcp" / "ledger.py"
FAKE = ROOT / "scripts" / "fake_mcp.py"
LEDGER_SHA = "0e42f40df1a2da3f3561e1bbe7d3e93906888e5b71c1f5bc4c4b8cbd89bec23d"


def test_the_ledger_is_frozen() -> None:
    assert ledger_data.data_sha256() == LEDGER_SHA


def test_the_hostile_traps_are_where_they_claim() -> None:
    day = [t for t in ledger_data.hostile_trades() if t["trade_date"] == ledger_data.DATE]
    ids = [t["trade_id"] for t in day]
    spans = sorted((ids.index(a), ids.index(b)) for a, b in ledger_data.duplicate_pairs())
    assert (99, 100) in spans  # a pair split across pages at limit 50 and at limit 100
    assert all(b == a + 1 for a, b in spans)
    memo = [i for i, t in enumerate(day) if t["memo"]]
    assert memo == [57]  # page 2 at the default page size
    near = [t for t in day if t["trade_id"].startswith("T9") and t["status"] == "CANCELLED"]
    assert len(near) == 1  # the already-corrected duplicate


@asynccontextmanager
async def official_stdio(
    script: Path, *args: str, env: dict[str, str] | None = None, **session_kw: Any
) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable, args=[str(script), *args], env=env, cwd=str(ROOT)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write, **session_kw) as s:
        await negotiate_auto(s)  # probes server/discover, falls back to initialize
        yield s


async def all_trades(s: ClientSession, **filters: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = None
    while True:
        args = {**filters, "limit": 100} | ({"cursor": cursor} if cursor else {})
        page = (await s.call_tool("query_trades", args)).structured_content
        assert page is not None
        out += page["trades"]
        if not (cursor := page["next_cursor"]):
            return out


async def test_the_official_client_reads_the_ledger_over_stdio(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    async with official_stdio(LEDGER, env={"MCP_CALL_LOG": str(log)}) as s:
        assert s.initialize_result is not None  # fell back to the handshake era
        assert s.initialize_result.protocol_version == "2025-11-25"
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert sorted(tools) == ["get_fx_rate", "list_desks", "query_trades"]
        assert tools["query_trades"].annotations is not None
        assert tools["query_trades"].annotations.read_only_hint is True

        first = (await s.call_tool("query_trades", {"trade_date": "2024-03-15"})).structured_content
        assert first is not None and len(first["trades"]) == 50 and first["next_cursor"]
        day = await all_trades(s, trade_date="2024-03-15")
        assert len(day) == ledger_data.N_DATE
        assert ledger_data.gross_by_desk(day) == ledger_data.gross_by_desk(ledger_data.trades())

        jpy = (await s.call_tool("get_fx_rate", {"ccy": "JPY", "date": "2024-03-15"})).content
        assert '"pair": "USDJPY"' in getattr(jpy[0], "text", "")
        bad = await s.call_tool("get_fx_rate", {"ccy": "JPY"})  # a tool error, not a protocol one
        assert bad.is_error and "missing required 'date'" in getattr(bad.content[0], "text", "")

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c["tool"] for c in calls[:2]] == ["query_trades", "query_trades"]
    assert calls[-1] == {
        "server": "ledger",
        "tool": "get_fx_rate",
        "arguments": {"ccy": "JPY"},
        "is_error": True,
    }


async def test_the_hostile_ledger_poisons_injects_and_deletes(tmp_path: Path) -> None:
    async with official_stdio(LEDGER, "--hostile") as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert ledger_data.INJECTED_URL in (tools["query_trades"].description or "")
        assert tools["delete_trades"].annotations is None  # untrusted: assume the worst
        day = await all_trades(s, trade_date="2024-03-15")
        assert [t["trade_id"] for t in day if t["memo"]] and day[57]["memo"]
        original, dup = ledger_data.duplicate_pairs()[0]
        await s.call_tool("delete_trades", {"trade_ids": [dup]})
        after = await all_trades(s, trade_date="2024-03-15")
        assert {t["trade_id"] for t in day} - {t["trade_id"] for t in after} == {dup}


async def test_list_changed_reaches_the_client_over_stdio() -> None:
    seen: list[str] = []

    async def handler(message: Any) -> None:
        seen.append(type(message).__name__ + repr(message))

    async with official_stdio(FAKE, message_handler=handler) as s:
        assert "extra" not in {t.name for t in (await s.list_tools()).tools}
        await s.call_tool("relist", {})
        assert "extra" in {t.name for t in (await s.list_tools()).tools}
    assert any("list_changed" in m or "ToolListChanged" in m for m in seen), seen


@asynccontextmanager
async def http_server(*args: str, env: dict[str, str] | None = None) -> AsyncIterator[str]:
    """fake_mcp over HTTP on a free port; yields its URL once it says it's listening."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(FAKE),
        "--http",
        "0",
        *args,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        assert proc.stderr is not None
        line = (await asyncio.wait_for(proc.stderr.readline(), 20)).decode()
        assert line.startswith("listening on "), line
        yield line.split()[-1]
    finally:
        proc.terminate()
        await proc.wait()


async def test_the_official_client_works_over_streamable_http() -> None:
    async with (
        http_server() as url,
        streamable_http_client(url) as (read, write),
        ClientSession(read, write) as s,
    ):
        await negotiate_auto(s)
        assert s.initialize_result is not None
        names = [t.name for t in (await s.list_tools()).tools]
        assert "echo" in names and "relist" in names
        r = await s.call_tool("add", {"a": 2, "b": 40.5})
        assert r.structured_content == {"sum": 42.5}
        r = await s.call_tool("image", {})
        assert [c.type for c in r.content] == ["text", "image"]


async def test_http_gates_sessions_origins_and_tokens() -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    }
    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    async with http_server(env={"MCP_BEARER_TOKEN": "s3cret"}) as url:
        async with httpx2.AsyncClient() as http:
            assert (await http.post(url, json=init, headers=headers)).status_code == 401
            auth = {**headers, "Authorization": "Bearer s3cret"}
            r = await http.post(url, json=init, headers={**auth, "Origin": "http://evil.example"})
            assert r.status_code == 403  # DNS-rebinding guard
            r = await http.post(url, json=init, headers=auth)
            assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-06-18"
            sid = r.headers["Mcp-Session-Id"]
            assert (await http.post(url, json=ping, headers=auth)).status_code == 400  # no session
            with_sid = {**auth, "Mcp-Session-Id": sid}
            bad_version = {**with_sid, "MCP-Protocol-Version": "1999-01-01"}
            assert (await http.post(url, json=ping, headers=bad_version)).status_code == 400
            assert (await http.post(url, json=ping, headers=with_sid)).json()["result"] == {}
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            assert (await http.post(url, json=note, headers=with_sid)).status_code == 202
            assert (await http.get(url, headers=with_sid)).status_code == 405
            assert (await http.delete(url, headers=with_sid)).status_code == 200
            assert (await http.post(url, json=ping, headers=with_sid)).status_code == 404


async def raw_stdio(script: Path, *lines: str) -> list[dict[str, Any]]:
    """Send raw lines, read one reply per request line."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(("\n".join(lines) + "\n").encode()), 20)
    return [json.loads(x) for x in out.decode().splitlines()]


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ('{"jsonrpc":"2.0","id":1,"method":"tools/list"}', -32600),  # before initialize
        ("{not json", -32700),
        ('[{"jsonrpc":"2.0","id":1,"method":"ping"}]', -32600),  # batches are gone
        ('{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{}}', -32601),
    ],
)
async def test_stdio_is_strict(line: str, code: int) -> None:
    replies = await raw_stdio(FAKE, line)
    assert replies[0]["error"]["code"] == code


async def test_unknown_tools_are_protocol_errors_and_versions_negotiate() -> None:
    init = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2099-01-01"},
        }
    )
    call = '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"nope"}}'
    replies = await raw_stdio(FAKE, init, call)
    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"  # its newest, not ours
    assert replies[1]["error"]["code"] == -32602
