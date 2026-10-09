"""A misbehaving MCP server for kama's MCP client tests (S7 part 2), on the eval kit.

    python scripts/fake_mcp.py [--http PORT] [--page-size N] [--protocol VERSION]
                               [--slow-start SECONDS] [--stderr-noise] [--untrusted]

Tools: echo, add (structuredContent), fail (isError), slow (sleeps, for timeouts and
cancellation), crash (the process exits mid-call), big (a result over any cap), image,
resource (embedded resource + resource_link), relist (adds a tool and sends
notifications/tools/list_changed; stdio only), drift (changes echo's description:
a "rug pull").

--untrusted strips every annotation; --protocol pins the one version it speaks;
--stderr-noise writes to stderr on every call (a client must not mix it into results).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `evals`
from evals.mcp.kit import (  # noqa: E402
    HANDSHAKE_VERSIONS,
    Server,
    Tool,
    ToolFailure,
    main,
    schema,
    text_result,
)

# 1x1 transparent PNG
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}


def build(argv: list[str]) -> Server:
    def arg(flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else None

    noisy = "--stderr-noise" in argv
    server: Server

    def noise(name: str) -> None:
        if noisy:
            print(f"fake_mcp: handling {name}", file=sys.stderr, flush=True)

    def echo(a: dict[str, Any]) -> str:
        noise("echo")
        return str(a["text"])

    def add(a: dict[str, Any]) -> dict[str, Any]:
        noise("add")
        total = a["a"] + a["b"]
        return text_result(str(total), structured={"sum": total})

    def fail(a: dict[str, Any]) -> str:
        raise ToolFailure(a.get("message", "it failed"))

    def slow(a: dict[str, Any]) -> str:
        time.sleep(float(a["seconds"]))
        return f"slept {a['seconds']}s"

    def crash(a: dict[str, Any]) -> str:
        os._exit(int(a.get("code", 3)))

    def big(a: dict[str, Any]) -> str:
        n = int(a["chars"])
        line = "0123456789abcdef" * 4 + "\n"
        return (line * (n // len(line) + 1))[:n]

    def image(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": "a 1x1 pixel"},
                {"type": "image", "data": PIXEL, "mimeType": "image/png"},
            ],
            "isError": False,
        }

    def resource(_: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "file:///fake/readme.txt",
                        "mimeType": "text/plain",
                        "text": "embedded text",
                    },
                },
                {"type": "resource_link", "uri": "file:///fake/big.log", "name": "big.log"},
            ],
            "isError": False,
        }

    def relist(_: dict[str, Any]) -> str:
        server.set_tool(Tool("extra", "Added at run time.", schema({}), lambda _: "extra!"))
        return "added tool `extra`"

    def drift(_: dict[str, Any]) -> str:
        old = server.tools["echo"]
        server.set_tool(Tool(old.name, old.description + " (v2)", old.input_schema, echo))
        return "echo's description changed"

    tools = [
        Tool("echo", "Echo the text back.", schema({"text": {"type": "string"}}, ("text",)), echo),
        Tool(
            "add",
            "Add two numbers.",
            schema({"a": {"type": "number"}, "b": {"type": "number"}}, ("a", "b")),
            add,
        ),
        Tool("fail", "Always fails.", schema({"message": {"type": "string"}}), fail),
        Tool(
            "slow",
            "Sleep, then answer.",
            schema({"seconds": {"type": "number"}}, ("seconds",)),
            slow,
        ),
        Tool("crash", "Exit the server process.", schema({"code": {"type": "integer"}}), crash),
        Tool("big", "A long text result.", schema({"chars": {"type": "integer"}}, ("chars",)), big),
        Tool("image", "An image result.", schema({}), image),
        Tool("resource", "Resource results.", schema({}), resource),
        Tool("relist", "Add a tool and notify the client.", schema({}), relist),
        Tool("drift", "Change echo's definition.", schema({}), drift),
    ]
    for t in tools:
        if t.name in ("echo", "add", "image", "resource"):
            t.annotations = READ_ONLY
        elif t.name == "crash":
            t.annotations = {"destructiveHint": True}
    if "--untrusted" in argv:
        for t in tools:
            t.annotations = None
    page = arg("--page-size")
    pinned = arg("--protocol")
    server = Server(
        "fake",
        tools,
        page_size=int(page) if page else None,
        versions=(pinned,) if pinned else HANDSHAKE_VERSIONS,
    )
    return server


if __name__ == "__main__":
    if delay := (
        sys.argv[sys.argv.index("--slow-start") + 1] if "--slow-start" in sys.argv else None
    ):
        time.sleep(float(delay))
    main(build(sys.argv), sys.argv)
