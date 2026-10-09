"""A small, strict MCP server: the eval tasks' servers and scripts/fake_mcp.py are built on
it. Handshake-era protocol (initialize / initialized), over stdio or Streamable HTTP.

Written by hand, like kama's own JSON-RPC bus, and checked against the official SDK's
client (tests/unit/test_mcp_servers.py). It is strict on purpose: kama's client (S7 part
2) is tested against it, so a request before the handshake, a missing session header
or a bad Origin is an error here, not something quietly tolerated.

The newest revision (2026-07-28) replaces the handshake with a stateless per-request
envelope and a `server/discover` probe. This server answers the probe with "method not
found", which is how a client learns to fall back to `initialize`.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HANDSHAKE_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")  # newest first

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

Result = dict[str, Any]


class ToolFailure(Exception):
    """A tool's own failure: reported as an isError result the model can read, not as a
    protocol error."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Result | str]
    annotations: dict[str, Any] | None = None
    title: str | None = None

    def spec(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            out["title"] = self.title
        if self.annotations is not None:
            out["annotations"] = self.annotations
        return out


def text_result(text: str, *, structured: Any = None, is_error: bool = False) -> Result:
    out: Result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def json_result(value: Any) -> Result:
    """Text (what most clients show the model) plus the same value as structuredContent."""
    return text_result(json.dumps(value, indent=1), structured=value)


def schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate(value: Any, sch: dict[str, Any], where: str = "arguments") -> list[str]:
    """The JSON Schema subset the tools here use. Returns problems (empty = valid)."""
    t = sch.get("type")
    if t is not None:
        # bool is an int in Python, not in JSON
        if not isinstance(value, _TYPES[t]) or (t != "boolean" and isinstance(value, bool)):
            return [f"{where}: expected {t}, got {type(value).__name__}"]
    problems: list[str] = []
    if "enum" in sch and value not in sch["enum"]:
        problems.append(f"{where}: must be one of {sch['enum']}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in sch and value < sch["minimum"]:
            problems.append(f"{where}: must be >= {sch['minimum']}")
        if "maximum" in sch and value > sch["maximum"]:
            problems.append(f"{where}: must be <= {sch['maximum']}")
    if isinstance(value, list) and "items" in sch:
        for i, item in enumerate(value):
            problems += validate(item, sch["items"], f"{where}[{i}]")
        if "maxItems" in sch and len(value) > sch["maxItems"]:
            problems.append(f"{where}: at most {sch['maxItems']} items")
    if isinstance(value, dict):
        props = sch.get("properties", {})
        for key in sch.get("required", []):
            if key not in value:
                problems.append(f"{where}: missing required {key!r}")
        for key, item in value.items():
            if key in props:
                problems += validate(item, props[key], f"{where}.{key}")
            elif sch.get("additionalProperties") is False:
                problems.append(f"{where}: unexpected {key!r}")
    return problems


@dataclass
class _Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    version: str | None = None  # negotiated in initialize
    initialized: bool = False  # notifications/initialized received


def _error(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": err}


def _ok(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class Server:
    def __init__(
        self,
        name: str,
        tools: list[Tool],
        *,
        version: str = "1.0.0",
        instructions: str | None = None,
        page_size: int | None = None,
        versions: tuple[str, ...] = HANDSHAKE_VERSIONS,
        call_log: Path | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.page_size = page_size
        self.versions = versions
        self.tools: dict[str, Tool] = {t.name: t for t in tools}
        log = call_log or (Path(p) if (p := os.environ.get("MCP_CALL_LOG")) else None)
        self.call_log = log
        self.cancelled: list[Any] = []  # request ids the client cancelled
        self._lock = threading.Lock()
        # Set by the stdio transport: sends a notification (HTTP has no stream for them).
        self.notify: Callable[[dict[str, Any]], None] | None = None

    # ---- tools that change at run time

    def set_tool(self, tool: Tool) -> None:
        self.tools[tool.name] = tool
        self._list_changed()

    def remove_tool(self, name: str) -> None:
        self.tools.pop(name, None)
        self._list_changed()

    def _list_changed(self) -> None:
        if self.notify is not None:
            self.notify({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    # ---- dispatch

    def handle_line(self, line: str, session: _Session) -> str | None:
        """One NDJSON line in, at most one line out."""
        try:
            msg = json.loads(line)
        except ValueError:
            return json.dumps(_error(None, PARSE_ERROR, "Parse error"))
        out = self.dispatch(msg, session)
        return None if out is None else json.dumps(out)

    def dispatch(self, msg: Any, session: _Session) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            # batches were removed in 2025-06-18
            return _error(None, INVALID_REQUEST, "Invalid Request")
        method = msg.get("method")
        rid = msg.get("id")
        is_request = "id" in msg
        if not isinstance(method, str):
            # a response to a request of ours (we send none), or junk
            return None if "result" in msg or "error" in msg else _error(rid, INVALID_REQUEST, "")
        params = msg.get("params") or {}
        if not is_request:
            self._notification(method, params, session)
            return None
        if method == "ping":
            return _ok(rid, {})
        if method == "initialize":
            return _ok(rid, self._initialize(params, session))
        handlers = {"tools/list": self._tools_list, "tools/call": self._tools_call}
        if method not in handlers:  # server/discover included: that's how a client falls back
            return _error(rid, METHOD_NOT_FOUND, f"Method not found: {method}")
        if session.version is None:
            return _error(rid, INVALID_REQUEST, f"{method} before initialize")
        return handlers[method](rid, params)

    def _notification(self, method: str, params: dict[str, Any], session: _Session) -> None:
        if method == "notifications/initialized":
            session.initialized = True
        elif method == "notifications/cancelled":
            self.cancelled.append(params.get("requestId"))

    def _initialize(self, params: dict[str, Any], session: _Session) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        session.version = asked if asked in self.versions else self.versions[0]
        out: dict[str, Any] = {
            "protocolVersion": session.version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    def _tools_list(self, rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        specs = [t.spec() for t in self.tools.values()]
        start = 0
        if (cursor := params.get("cursor")) is not None:
            try:
                start = int(base64.urlsafe_b64decode(str(cursor)).decode())
            except ValueError:
                return _error(rid, INVALID_PARAMS, "Invalid cursor")
        if self.page_size is None:
            return _ok(rid, {"tools": specs[start:]})
        end = start + self.page_size
        out: dict[str, Any] = {"tools": specs[start:end]}
        if end < len(specs):
            out["nextCursor"] = base64.urlsafe_b64encode(str(end).encode()).decode()
        return _ok(rid, out)

    def _tools_call(self, rid: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return _error(rid, INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if problems := validate(args, tool.input_schema):
            # an input error is a tool error: the model can read it and fix the call
            result = text_result("Invalid arguments: " + "; ".join(problems), is_error=True)
        else:
            try:
                out = tool.handler(args)
                result = text_result(out) if isinstance(out, str) else out
            except ToolFailure as e:
                result = text_result(str(e), is_error=True)
        self._log_call(tool.name, args, bool(result.get("isError")))
        return _ok(rid, result)

    def _log_call(self, tool: str, args: dict[str, Any], is_error: bool) -> None:
        """What reached the server: graders read this, never the agent's account of it."""
        if self.call_log is None:
            return
        row = {"server": self.name, "tool": tool, "arguments": args, "is_error": is_error}
        with self._lock, self.call_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ---- transports

    def serve_stdio(self) -> None:
        session = _Session()
        out = sys.stdout

        def send(msg: dict[str, Any]) -> None:
            out.write(json.dumps(msg) + "\n")
            out.flush()

        self.notify = send
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            reply = self.handle_line(line, session)
            if reply is not None:
                out.write(reply + "\n")
                out.flush()

    def serve_http(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Streamable HTTP at /mcp. Answers with application/json (no SSE stream: this
        server sends no notifications over HTTP). Prints the URL to stderr once bound."""
        httpd = ThreadingHTTPServer((host, port), _handler_for(self))
        print(f"listening on http://{host}:{httpd.server_port}/mcp", file=sys.stderr, flush=True)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


def _handler_for(server: Server) -> type[BaseHTTPRequestHandler]:
    sessions: dict[str, _Session] = {}
    token = os.environ.get("MCP_BEARER_TOKEN")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        body = b""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send(self, status: int, body: dict[str, Any] | None = None, **headers: str) -> None:
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _gate(self) -> bool:
            """Origin (DNS rebinding), auth and path: False once an error is sent. The body
            is read first: left unread on a keep-alive connection, it would be parsed as
            the next request."""
            self.body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            origin = self.headers.get("Origin")
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                self._send(403, _error(None, INVALID_REQUEST, "Origin not allowed"))
                return False
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, None, WWW_Authenticate="Bearer")
                return False
            if self.path.split("?")[0] != "/mcp":
                self._send(404)
                return False
            return True

        def _session(self) -> _Session | None:
            sid = self.headers.get("Mcp-Session-Id")
            if sid is None:
                self._send(400, _error(None, INVALID_REQUEST, "Missing Mcp-Session-Id"))
                return None
            if sid not in sessions:
                self._send(404, _error(None, INVALID_REQUEST, "Unknown session"))
                return None
            version = self.headers.get("MCP-Protocol-Version")
            if version is not None and version not in server.versions:
                self._send(400, _error(None, INVALID_REQUEST, f"Unsupported version {version}"))
                return None
            return sessions[sid]

        def do_POST(self) -> None:  # noqa: N802
            if not self._gate():
                return
            if "application/json" not in self.headers.get("Accept", ""):
                self._send(406, _error(None, INVALID_REQUEST, "Accept application/json"))
                return
            try:
                msg = json.loads(self.body)
            except ValueError:
                self._send(400, _error(None, PARSE_ERROR, "Parse error"))
                return
            if isinstance(msg, dict) and msg.get("method") == "initialize" and "id" in msg:
                session = _Session()
                reply = server.dispatch(msg, session)
                sessions[session.id] = session
                self._send(200, reply, Mcp_Session_Id=session.id)
                return
            maybe = self._session()
            if maybe is None:
                return
            reply = server.dispatch(msg, maybe)
            if reply is None:
                self._send(202)
            else:
                self._send(200, reply)

        def do_GET(self) -> None:  # noqa: N802
            if self._gate():
                self._send(405, None, Allow="POST, DELETE")

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._gate():
                return
            if self._session() is not None:
                sessions.pop(self.headers["Mcp-Session-Id"], None)
                self._send(200)

    return Handler


def main(server: Server, argv: list[str]) -> None:
    """`--http PORT` (0 = any free port) serves HTTP; otherwise stdio."""
    if "--http" in argv:
        server.serve_http(port=int(argv[argv.index("--http") + 1]))
    else:
        server.serve_stdio()
