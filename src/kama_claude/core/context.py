"""Token accounting for a run's requests (S6).

The size of the request just sent is exact: the response's usage says it (input tokens
are only the uncached tail, so add cache reads and cache writes). The size of the *next*
request is that, plus the reply, plus whatever was appended since (tool results,
notices), which hasn't been measured: that part is estimated at 3 chars per token, on
the high side for English and code. Near the budget the estimate isn't good enough to
decide on (part 3 compacts on it), so the meter asks the API for an exact count.

Every llm.call span records the estimate and the actual size, so the estimator's error
is measured, not assumed.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from typing import Any, Protocol, runtime_checkable

from kama_claude.core.llm.types import LLMResponse, Message, ToolSpec, Usage

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 3.0
EXACT_ABOVE = 0.85  # below this share of the budget the estimate decides alone


def request_tokens(usage: Usage) -> int:
    return usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens


def estimate_tokens(content: Any) -> int:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@runtime_checkable
class TokenCounter(Protocol):
    async def count_tokens(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int: ...


@runtime_checkable
class Compactor(Protocol):
    """A provider that can summarize a conversation server-side (S6, on-demand)."""

    async def compact(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec], instructions: str
    ) -> LLMResponse: ...


# What the server's summarizer is told to keep. Exact values matter more than prose:
# the agent continues from this text alone (the replaced messages are gone).
COMPACTION_INSTRUCTIONS = """\
Summarize this agent session so the agent can continue the task from your summary
alone: everything before it will be removed. Keep, exactly as observed:
- the user's requests and every constraint or preference they stated;
- files read, created, changed or deleted, and what changed in each;
- commands run and their key results: numbers, identifiers, timestamps, counts and
  names copied exactly (never rounded or paraphrased);
- facts established and where they came from, and conventions discovered (units,
  signs, defaults, formats);
- ids of cut tool outputs ("saved as output ...") that may still be needed;
- errors still open, decisions made and why;
- what is done, and what is left, in order.
Do not invent anything. Do not call tools: respond with the summary text only."""


class ContextMeter:
    def __init__(self, budget: int, counter: TokenCounter | None = None) -> None:
        self.budget = budget
        self._counter = counter
        self.sizes: list[int] = []  # every request sent, exact
        self._last = 0  # the last request, exact
        self._last_output = 0
        self._sent = 0  # how many messages the last request carried
        self._fresh = True  # nothing sent since the start (or the last compaction)

    def estimate(self, system: str, tools: list[ToolSpec], messages: list[Message]) -> int:
        """The next request's size: exact up to the last request, estimated after it."""
        if self._fresh:
            return estimate_tokens(system) + estimate_tokens(tools) + estimate_tokens(messages)
        # messages[self._sent] is the reply to the last request: its output tokens.
        return self._last + self._last_output + estimate_tokens(messages[self._sent + 1 :])

    async def measure(
        self, system: str, tools: list[ToolSpec], messages: list[Message]
    ) -> tuple[int, str]:
        """(tokens, how): the estimate, or an exact count when the estimate is near the
        budget and a counter is available. A failed count falls back to the estimate."""
        est = self.estimate(system, tools, messages)
        if self._counter is None or est < self.budget * EXACT_ABOVE:
            return est, "estimate"
        try:
            return await self._counter.count_tokens(
                system=system, messages=messages, tools=tools
            ), "count"
        except Exception:
            logger.warning("token count failed; using the estimate", exc_info=True)
            return est, "estimate"

    def observe(self, usage: Usage, messages_sent: int) -> int:
        """Record a response: the request it answered was exactly this big."""
        self._last = request_tokens(usage)
        self._last_output = usage.output_tokens
        self._sent = messages_sent
        self._fresh = False
        self.sizes.append(self._last)
        return self._last

    def restart(self) -> None:
        """After a compaction the messages were replaced: estimate from scratch again."""
        self._fresh = True

    @property
    def peak(self) -> int:
        return max(self.sizes, default=0)

    @property
    def mean(self) -> int:
        return round(statistics.mean(self.sizes)) if self.sizes else 0
