"""Which MCP servers a run starts: `~/.kama/mcp.toml` (KAMA_MCP_FILE).

    [servers.ledger]
    command = ["python3", "ledger.py"]     # stdio: spawned per run, cwd = the workspace
    env = { LEDGER_DB = "/data/ledger.db" }
    trust = false                          # annotations may only tighten the policy

    [servers.search]
    transport = "http"
    url = "https://mcp.example.com/mcp"
    bearer_env = "SEARCH_MCP_TOKEN"        # the token is read from this env var, never stored

The file is the user's: it decides which processes start, so no tool may read or write
it (it is one of the daemon's own paths). A workspace's servers need the user's approval
of their exact command (S7 part 2).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# Tool names become mcp__<server>__<tool>, so a server name can't contain "__" (the name
# would be ambiguous) and stays short (the API caps a tool name's length).
SERVER_NAME = re.compile(r"^[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*$")
SERVER_NAME_MAX = 24


class McpConfigError(ValueError):
    pass


class McpServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: Literal["stdio", "http"] = "stdio"
    command: list[str] | None = None  # stdio: argv
    cwd: Path | None = None  # stdio: default is the workspace
    env: dict[str, str] = Field(default_factory=dict)  # stdio: added to a scrubbed env
    url: str | None = None  # http: the Streamable HTTP endpoint
    bearer_env: str | None = None  # http: env var holding a bearer token
    # Trusted servers' annotations (readOnlyHint, ...) can relax the policy; untrusted
    # ones can only make it stricter.
    trust: bool = False
    startup_timeout_s: float = Field(default=15, gt=0)
    call_timeout_s: float = Field(default=120, gt=0)

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerConfig:
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio server needs `command` (a list: program and args)")
            if self.url or self.bearer_env:
                raise ValueError("`url` and `bearer_env` are for http servers")
        else:
            if not self.url or not self.url.startswith(("http://", "https://")):
                raise ValueError("an http server needs `url` (http:// or https://)")
            if self.command or self.cwd or self.env:
                raise ValueError("`command`, `cwd` and `env` are for stdio servers")
        return self


class McpFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> McpFile:
        for name in self.servers:
            if len(name) > SERVER_NAME_MAX or not SERVER_NAME.match(name):
                raise ValueError(
                    f"server name {name!r}: letters and digits, single - or _ between them, "
                    f"at most {SERVER_NAME_MAX} characters"
                )
        return self


def load_mcp_file(path: Path) -> McpFile:
    """The servers in `path`; none if the file doesn't exist. Raises McpConfigError."""
    path = path.expanduser()
    if not path.is_file():
        return McpFile()
    try:
        return McpFile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as e:
        raise McpConfigError(f"{path}: {e}") from e


def _toml_str(s: str) -> str:
    # A JSON string is a valid TOML basic string (same escapes, \uXXXX included).
    return json.dumps(s)


def dump_mcp_file(f: McpFile) -> str:
    """TOML that load_mcp_file reads back to an equal McpFile (the eval harness writes
    one per trial)."""
    lines: list[str] = []
    for name, s in f.servers.items():
        lines.append(f"[servers.{_toml_str(name)}]")
        for key, value in s.model_dump(exclude_defaults=True).items():
            if key == "env":
                pairs = ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in value.items())
                lines.append(f"env = {{ {pairs} }}")
            elif isinstance(value, list):
                lines.append(f"{key} = [{', '.join(_toml_str(x) for x in value)}]")
            elif isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int | float):
                lines.append(f"{key} = {value}")
            else:
                lines.append(f"{key} = {_toml_str(str(value))}")
        lines.append("")
    return "\n".join(lines)
