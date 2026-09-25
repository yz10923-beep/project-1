"""NDJSON framing shared by server and client."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

# Upper bound on one frame. StreamReader.readline raises ValueError past this,
# which protects the daemon from a client that never sends a newline.
MAX_FRAME_BYTES = 1024 * 1024


class FrameTooLarge(Exception):
    pass


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Read one newline-terminated frame. Returns None on clean EOF."""
    try:
        line = await reader.readline()
    except ValueError as e:  # limit overrun; the stream is unusable afterwards
        raise FrameTooLarge(str(e)) from e
    if not line:
        return None
    return line


async def write_frame(writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    """Serialize a model as one line and flush it."""
    writer.write(msg.model_dump_json().encode() + b"\n")
    await writer.drain()
