"""Scripted LLM provider: returns canned responses in order and records every request."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from kama_claude.core.llm.types import LLMError, LLMResponse, Message, StopReason, ToolSpec, Usage


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
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> LLMResponse:
        # Deep copy: the loop keeps appending to the same list, and we want a snapshot.
        self.requests.append(Request(system, copy.deepcopy(messages), tools))
        if not self.script:
            raise AssertionError("provider called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, LLMError):
            raise item
        return item
