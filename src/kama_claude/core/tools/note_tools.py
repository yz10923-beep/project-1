"""note_save / note_update / note_delete / note_list: the agent's durable memory.

Save what a later run in this workspace would otherwise have to rediscover; mark values
that change as volatile and say where they came from. The notes come back at the start
of later runs (core/notes.py: memory_preamble).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from kama_claude.core.notes import MAX_NOTE_CHARS, NoteBook, NoteError, NoteScope, render_note
from kama_claude.core.tools.base import Tool, ToolContext, ToolError, ToolResult

NOTE_TOOL_NAMES = frozenset({"note_save", "note_update", "note_delete", "note_list"})
NoteText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NOTE_CHARS)
]
NoteId = Annotated[str, StringConstraints(pattern=r"^[ws]\d+$")]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _book(ctx: ToolContext) -> NoteBook:
    if ctx.notes is None:
        raise ToolError("memory is off for this run")
    return ctx.notes


class NoteSaveParams(_Params):
    text: NoteText = Field(description="One self-contained fact, e.g. 'Tests need RISK_DB=...'.")
    scope: NoteScope = Field(
        default="workspace",
        description="workspace: useful to any later run here; session: only this conversation.",
    )
    source: str = Field(
        default="", max_length=200, description="Where it came from: a file, command, the user."
    )
    volatile: bool = Field(
        default=False,
        description="True for values that change (prices, rates, refreshed files): later runs "
        "re-check them at the source.",
    )


class NoteSave(Tool[NoteSaveParams]):
    name = "note_save"
    description = (
        "Remember a fact for later runs in this workspace: how to build or test the project, "
        "where things are, decisions the user made. Not secrets, and not what the files "
        "already say plainly. Returns the note's id."
    )
    params_model = NoteSaveParams

    async def run(self, params: NoteSaveParams, ctx: ToolContext) -> ToolResult:
        book = _book(ctx)
        try:
            note = await asyncio.to_thread(
                book.save, params.text, params.scope, params.source, params.volatile
            )
        except NoteError as e:
            raise ToolError(str(e), "rejected") from e
        return ToolResult(f"Saved [{note.id}] ({note.scope}).")


class NoteUpdateParams(_Params):
    id: NoteId = Field(description="Note id, e.g. w3.")
    text: NoteText | None = None
    source: str | None = Field(default=None, max_length=200)
    volatile: bool | None = None


class NoteUpdate(Tool[NoteUpdateParams]):
    name = "note_update"
    description = "Correct or refresh a note (e.g. a value that changed since it was saved)."
    params_model = NoteUpdateParams

    async def run(self, params: NoteUpdateParams, ctx: ToolContext) -> ToolResult:
        book = _book(ctx)
        try:
            note = await asyncio.to_thread(
                book.update, params.id, params.text, params.source, params.volatile
            )
        except NoteError as e:
            raise ToolError(str(e), "rejected") from e
        return ToolResult(f"Updated: {render_note(note, datetime.now(UTC))}")


class NoteDeleteParams(_Params):
    id: NoteId
    reason: str = Field(min_length=1, max_length=200, description="Why it no longer holds.")


class NoteDelete(Tool[NoteDeleteParams]):
    name = "note_delete"
    description = "Forget a note that turned out wrong, stale or duplicated."
    params_model = NoteDeleteParams

    async def run(self, params: NoteDeleteParams, ctx: ToolContext) -> ToolResult:
        book = _book(ctx)
        try:
            note = await asyncio.to_thread(book.delete, params.id, params.reason)
        except NoteError as e:
            raise ToolError(str(e), "rejected") from e
        return ToolResult(f"Deleted [{note.id}].")


class NoteListParams(_Params):
    pass


class NoteList(Tool[NoteListParams]):
    name = "note_list"
    description = "Show every note this run can see, with ids, sources and age."
    params_model = NoteListParams

    async def run(self, params: NoteListParams, ctx: ToolContext) -> ToolResult:
        notes = await asyncio.to_thread(_book(ctx).visible)
        if not notes:
            return ToolResult("No notes yet.")
        now = datetime.now(UTC)
        return ToolResult("\n".join(render_note(n, now) for n in notes))


def note_tools() -> list[Tool[Any]]:
    return [NoteSave(), NoteUpdate(), NoteDelete(), NoteList()]
