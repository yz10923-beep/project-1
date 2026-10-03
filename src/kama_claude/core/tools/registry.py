from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.tools.base import Tool, ToolContext, ToolError, ToolResult

logger = logging.getLogger(__name__)

# Upper bound on what one tool result may put into the context window.
MAX_RESULT_CHARS = 30_000


def truncate_middle(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """Keep head and tail: errors and summaries tend to sit at the end of output."""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n\n[... {omitted} characters truncated ...]\n\n{text[-half:]}"


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]]) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any] | None:
        return self._tools.get(name)

    def specs(self) -> list[dict[str, Any]]:
        # Sorted so the tools block is byte-identical across requests (prompt-cache prefix).
        return [self._tools[name].spec() for name in sorted(self._tools)]

    async def execute(self, name: str, raw_input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Run one tool call. Every failure becomes an is_error result the model can read,
        with an error_kind saying what went wrong (and so whether trying again can help)."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                f"Unknown tool: {name}. Available: {sorted(self._tools)}", True, "unknown_tool"
            )
        try:
            params = tool.params_model.model_validate(raw_input)
        except ValidationError as e:
            return ToolResult(
                format_validation_error(name, e, tool.params_model), True, "invalid_input"
            )
        try:
            result = await tool.run(params, ctx)
        except ToolError as e:
            return ToolResult(f"Error: {e}", True, e.kind)
        except Exception as e:
            # Unexpected bug in the tool: log the traceback, give the model a short message.
            logger.exception("tool %s crashed", name)
            return ToolResult(
                f"Tool {name} failed unexpectedly: {type(e).__name__}: {e}. This is a bug in "
                "the tool, not in your input; the same call will likely fail again.",
                True,
                "crashed",
            )
        content = truncate_middle(result.content)
        return ToolResult(content, result.is_error, result.error_kind if result.is_error else None)


def format_validation_error(name: str, e: ValidationError, model: type[BaseModel]) -> str:
    """One line per bad field, then the expected shape: shorter for the model than
    pydantic's dump, and it says what to send instead."""
    lines = [f"Invalid input for {name}:"]
    for err in e.errors():
        where = ".".join(str(x) for x in err["loc"]) or "(input)"
        got = ""
        if err["type"] not in {"missing", "extra_forbidden"} and "input" in err:
            got = f" (got {str(err['input'])[:60]!r})"
        lines.append(f"- {where}: {err['msg']}{got}")
    fields = []
    for fname, f in model.model_fields.items():
        ann = getattr(f.annotation, "__name__", str(f.annotation))
        fields.append(f'"{fname}"{"" if f.is_required() else "?"}: {ann}')
    lines.append("Expected: {" + ", ".join(fields) + "}. Fix the input and call again.")
    return "\n".join(lines)
