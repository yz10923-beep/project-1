from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from kama_claude.core.notes import NoteBook
from kama_claude.core.outputs import OutputCut, OutputStore
from kama_claude.core.plan import Plan
from kama_claude.core.sandbox import Sandbox


@dataclass(frozen=True)
class ToolContext:
    workspace: Path
    # One plan per run; only the task_* tools touch it.
    plan: Plan = field(default_factory=Plan)
    # The run's view of durable notes (S4); None when memory is off.
    notes: NoteBook | None = None
    # S5: where bash runs, and whether this call may use the network (the policy decides
    # per call). No sandbox = run directly, as before S5.
    sandbox: Sandbox | None = None
    network: bool = True
    env_keep: frozenset[str] = frozenset()
    hidden: tuple[Path, ...] = ()  # private paths the sandbox hides from bash (if it can)
    # S6: with a store, a result over the cap is saved whole and the model told how to
    # page through it; without one (KAMA_CONTEXT=false) the S1 middle cut applies.
    outputs: OutputStore | None = None
    max_result_chars: int = 30_000
    max_line_chars: int | None = None  # read_file / read_output cut longer lines (S6)


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False
    error_kind: str | None = None  # set when is_error: what kind of failure (S5)
    cut: OutputCut | None = None  # set when the result was cut at the cap (S6)


class ToolError(Exception):
    """Expected failure the model should see and can react to (bad path, missing file, ...)."""

    def __init__(self, message: str, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


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
        raise ToolError(f"path escapes the workspace: {path}", "outside_workspace")
    return target
