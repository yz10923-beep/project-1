"""read_output: page through a tool result that was cut at the cap (S6).

A cut result names its id in the notice; the whole text was saved outside the workspace
(core/outputs.py). Read-only and confined to this run's and its session's outputs, so it
needs no approval.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.outputs import LINE_MAX_CHARS, clip_line
from kama_claude.core.tools.base import Tool, ToolContext, ToolError, ToolResult

OUTPUT_TOOL_NAMES = frozenset({"read_output"})


class ReadOutputParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description='The output id from a cut result\'s notice, e.g. "toolu_01A...".')
    offset: int = Field(default=1, ge=1, description="1-based line number to start from.")
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum lines to return.")


class ReadOutput(Tool[ReadOutputParams]):
    name = "read_output"
    description = (
        "Read part of a tool result that was too long and was cut (the cut notice gives its "
        "id and which lines are missing). Returns lines prefixed with their 1-based numbers. "
        "Prefer narrowing the original command when you only need some of it."
    )
    params_model = ReadOutputParams

    async def run(self, params: ReadOutputParams, ctx: ToolContext) -> ToolResult:
        return await asyncio.to_thread(self._run_sync, params, ctx)

    def _run_sync(self, params: ReadOutputParams, ctx: ToolContext) -> ToolResult:
        path = ctx.outputs.find(params.id) if ctx.outputs is not None else None
        if path is None:
            raise ToolError(
                f'no saved output "{params.id}": only results cut in this conversation are '
                "kept, and their ids are in the cut notice",
                "not_found",
            )
        lines = path.read_text().splitlines()
        start = params.offset - 1
        chunk = lines[start : start + params.limit]
        if not chunk:
            raise ToolError(
                f"output {params.id} has {len(lines)} lines; nothing at offset {params.offset}",
                "invalid_input",
            )
        width = ctx.max_line_chars or LINE_MAX_CHARS
        body = "\n".join(
            f"{i:>6}\t{clip_line(line, width)}" for i, line in enumerate(chunk, params.offset)
        )
        end = start + len(chunk)
        if end < len(lines):
            body += f"\n(showing lines {params.offset}-{end} of {len(lines)})"
        return ToolResult(body)


def output_tools() -> list[Tool[ReadOutputParams]]:
    return [ReadOutput()]
