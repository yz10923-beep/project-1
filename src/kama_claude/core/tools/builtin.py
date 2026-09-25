"""Built-in tools: read_file, list_dir, write_file, bash."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    resolve_in_workspace,
)

_MAX_LIST_ENTRIES = 500


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileParams(_Params):
    path: str = Field(description="File path, relative to the workspace root.")
    offset: int = Field(default=1, ge=1, description="1-based line number to start from.")
    limit: int = Field(default=2000, ge=1, le=5000, description="Maximum lines to return.")


class ReadFile(Tool[ReadFileParams]):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace. Returns lines prefixed with their "
        "1-based line numbers. Use offset/limit to page through large files."
    )
    params_model = ReadFileParams

    async def run(self, params: ReadFileParams, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run_sync, params, ctx)

    def _run_sync(self, params: ReadFileParams, ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, params.path)
        if not path.is_file():
            raise ToolError(f"not a file: {params.path}")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as e:
            raise ToolError(f"not a UTF-8 text file: {params.path}") from e
        start = params.offset - 1
        chunk = lines[start : start + params.limit]
        if not chunk:
            return ToolResult(f"(file has {len(lines)} lines; nothing at offset {params.offset})")
        body = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start=params.offset))
        end = start + len(chunk)
        if end < len(lines):
            body += f"\n(showing lines {params.offset}-{end} of {len(lines)})"
        return ToolResult(body)


class ListDirParams(_Params):
    path: str = Field(default=".", description="Directory, relative to the workspace root.")


class ListDir(Tool[ListDirParams]):
    name = "list_dir"
    description = "List a workspace directory. Directories end with '/'."
    params_model = ListDirParams

    async def run(self, params: ListDirParams, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run_sync, params, ctx)

    def _run_sync(self, params: ListDirParams, ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, params.path)
        if not path.is_dir():
            raise ToolError(f"not a directory: {params.path}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        names = [e.name + ("/" if e.is_dir() else "") for e in entries[:_MAX_LIST_ENTRIES]]
        if len(entries) > _MAX_LIST_ENTRIES:
            names.append(f"(... {len(entries) - _MAX_LIST_ENTRIES} more entries)")
        return ToolResult("\n".join(names) if names else "(empty directory)")


class WriteFileParams(_Params):
    path: str = Field(description="File path, relative to the workspace root.")
    content: str = Field(description="Full new file content. Overwrites any existing file.")


class WriteFile(Tool[WriteFileParams]):
    name = "write_file"
    description = (
        "Create or overwrite a text file in the workspace with the given full content. "
        "Parent directories are created as needed."
    )
    params_model = WriteFileParams
    requires_approval = True

    async def run(self, params: WriteFileParams, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run_sync, params, ctx)

    def _run_sync(self, params: WriteFileParams, ctx: ToolContext) -> ToolResult:
        path = resolve_in_workspace(ctx.workspace, params.path)
        if path.is_dir():
            raise ToolError(f"is a directory: {params.path}")
        existed = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params.content, encoding="utf-8")
        verb = "Overwrote" if existed else "Created"
        return ToolResult(f"{verb} {params.path} ({len(params.content.encode())} bytes)")


class BashParams(_Params):
    command: str = Field(min_length=1, description="Shell command, run with the workspace as cwd.")
    timeout_s: int = Field(default=60, ge=1, le=600, description="Kill the command after this.")


class Bash(Tool[BashParams]):
    name = "bash"
    description = (
        "Run a shell command in the workspace directory (non-interactive; stdin is empty). "
        "Returns the exit code and combined stdout/stderr. A non-zero exit code is reported, "
        "not treated as a tool failure."
    )
    params_model = BashParams
    requires_approval = True

    async def run(self, params: BashParams, ctx: ToolContext) -> ToolResult:
        proc = await asyncio.create_subprocess_shell(
            params.command,
            cwd=ctx.workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Own process group, so a timeout kills the command's children too.
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=params.timeout_s)
        except TimeoutError:
            _kill_group(proc)
            await proc.wait()
            return ToolResult(f"Command timed out after {params.timeout_s}s and was killed.", True)
        except asyncio.CancelledError:
            _kill_group(proc)
            raise
        text = out.decode("utf-8", errors="replace")
        return ToolResult(f"exit_code: {proc.returncode}\n{text}".rstrip())


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def builtin_tools() -> list[Tool[Any]]:
    return [ReadFile(), ListDir(), WriteFile(), Bash()]
