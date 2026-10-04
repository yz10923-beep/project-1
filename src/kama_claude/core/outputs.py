"""Tool outputs cut at the cap, kept whole (S6).

Since S1 a tool result longer than the cap has been cut to its head and tail, and the
middle was lost. With context governance on, the full text is saved here instead, the
model is told the result's id and how to page through it (`read_output`), and the event
records the cut. The store lives in the run dir, outside the workspace: bash can't
reach it, only read_output can, and only for this run and earlier runs of its session.

The cut keeps whole lines where it can and says which line numbers are missing, so the
model can ask for exactly those.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LINE_MAX_CHARS = 2_000  # read_file and read_output cut longer lines (minified files, blobs)


class OutputCut(BaseModel):
    """What a tool.finished event records about a result cut at the cap."""

    output_id: str
    original_chars: int
    kept_chars: int
    total_lines: int


class OutputStore:
    def __init__(self, write_dir: Path, read_dirs: Sequence[Path] = ()) -> None:
        self.write_dir = write_dir
        # this run's outputs first, then earlier runs of the session, newest first
        self._dirs = [write_dir, *read_dirs]

    def save(self, output_id: str, text: str) -> None:
        """Blocking: call through asyncio.to_thread."""
        if not _ID.match(output_id):
            raise ValueError(f"bad output id: {output_id!r}")
        self.write_dir.mkdir(parents=True, exist_ok=True)
        (self.write_dir / f"{output_id}.txt").write_text(text)

    def find(self, output_id: str) -> Path | None:
        """The saved output, or None. Ids are a closed alphabet, so no path can escape."""
        if not _ID.match(output_id):
            return None
        for d in self._dirs:
            if (path := d / f"{output_id}.txt").is_file():
                return path
        return None


def line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def cut(text: str, limit: int, output_id: str) -> tuple[str, OutputCut]:
    """Head and tail of `text`, at line boundaries where possible, with a notice in the
    middle naming the missing lines and how to read them."""
    half = limit // 2
    head, tail = text[:half], text[-half:]
    if "\n" in head[:-1]:
        head = head[: head.rindex("\n", 0, len(head) - 1) + 1]
    if "\n" in tail[:-1]:
        tail = tail[tail.index("\n") + 1 :]
    total = line_count(text)
    first_missing = head.count("\n") + 1
    last_missing = total - line_count(tail)
    omitted = len(text) - len(head) - len(tail)
    span = (
        f"lines {first_missing}-{last_missing} of {total}"
        if last_missing >= first_missing
        else f"part of line {first_missing}"
    )
    notice = (
        ("" if head.endswith("\n") else "\n")
        + f"[... {omitted} characters cut here ({span}). The whole output ({len(text)} "
        f'characters, {total} lines) is saved as output "{output_id}": read any part with '
        f'read_output(id="{output_id}", offset=<line>, limit=<lines>), or narrow the command '
        "(grep -n, head, tail, wc -l, awk) to get only what you need. ...]\n"
    )
    shown = head + notice + tail
    return shown, OutputCut(
        output_id=output_id,
        original_chars=len(text),
        kept_chars=len(head) + len(tail),
        total_lines=total,
    )


def clip_line(line: str, limit: int = LINE_MAX_CHARS) -> str:
    if len(line) <= limit:
        return line
    return f"{line[:limit]} [... line cut: {len(line)} characters]"
