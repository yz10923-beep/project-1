"""Scripted LLM provider: returns canned responses in order and records every request."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any

from kama_claude.core.llm.types import (
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    TextCallback,
    ToolSpec,
    Usage,
)


def text_response(text: str, stop: StopReason = "end_turn") -> LLMResponse:
    return LLMResponse(
        stop_reason=stop,
        content=[{"type": "text", "text": text}],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def tool_response(*calls: tuple[str, str, dict[str, Any]], text: str = "") -> LLMResponse:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content += [{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in calls]
    return LLMResponse(
        stop_reason="tool_use", content=content, usage=Usage(input_tokens=10, output_tokens=5)
    )


@dataclass
class Request:
    system: str
    messages: list[Message]
    tools: list[ToolSpec]


@dataclass
class ScriptedProvider:
    script: list[LLMResponse | LLMError]
    model: str = "fake-model"
    requests: list[Request] = field(default_factory=list)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        # Deep copy: the loop keeps appending to the same list, and we want a snapshot.
        self.requests.append(Request(system, copy.deepcopy(messages), tools))
        if not self.script:
            raise AssertionError("provider called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, LLMError):
            raise item
        if on_text is not None:  # stream text word by word, like the real provider
            for block in item.content:
                if block.get("type") == "text":
                    for chunk in block["text"].split(" "):
                        await on_text(chunk + " ")
        # Like a real API, report the serving model on every response.
        return item if item.model else item.model_copy(update={"model": self.model})


@dataclass
class GatedProvider(ScriptedProvider):
    """Waits for `gate` before every response, so tests can act while a run is live."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        await self.gate.wait()
        return await super().complete(
            system=system, messages=messages, tools=tools, on_text=on_text
        )
