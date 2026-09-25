"""Provider-neutral request/response types.

Conversation history is kept in the Anthropic Messages wire format
(`{"role": ..., "content": [blocks]}`) because that format *is* the protocol:
the assistant's content blocks must be echoed back verbatim, including blocks we
don't interpret (thinking, fallback markers). So `LLMResponse.content` holds raw
block dicts, and the parsed fields are read-only conveniences over them.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

type Message = dict[str, Any]
type ToolSpec = dict[str, Any]

StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
    "other",
]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
        )


class ToolCall(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


class LLMResponse(BaseModel):
    stop_reason: StopReason
    content: list[dict[str, Any]] = Field(description="Raw content blocks, echoed back as-is.")
    usage: Usage = Field(default_factory=Usage)
    model: str = ""

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_calls(self) -> list[ToolCall]:
        return [
            ToolCall(id=b["id"], name=b["name"], input=b.get("input") or {})
            for b in self.content
            if b.get("type") == "tool_use"
        ]


class LLMError(Exception):
    """Provider call failed after the SDK's own retries. `retryable`: a later try may work."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    @property
    def model(self) -> str: ...

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse: ...
