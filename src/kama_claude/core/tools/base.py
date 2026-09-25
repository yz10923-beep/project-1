from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolContext:
    workspace: Path


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


class ToolError(Exception):
    """Expected failure the model should see and can react to (bad path, missing file, ...)."""


class Tool[P: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    params_model: ClassVar[type[BaseModel]]
    # Side-effecting tools must be approved before they run (S5 replaces this with policy).
    requires_approval: ClassVar[bool] = False

    def spec(self) -> dict[str, Any]:
        """Tool definition in Messages API shape."""
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}

    @abstractmethod
    async def run(self, params: P, ctx: ToolContext) -> ToolResult: ...


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    """Resolve `path` (relative or absolute) and refuse anything outside the workspace.

    resolve() follows symlinks, so a link pointing outside the workspace is rejected too.
    """
    root = workspace.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ToolError(f"path escapes the workspace: {path}")
    return target
