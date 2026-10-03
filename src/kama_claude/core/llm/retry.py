"""Retrying model calls (S5). The loop owns this, not the SDK, so every attempt is an
llm.retry event (clients can say "retrying in 4s: overloaded") and a span in the trace.

A model call has no side effects, so retrying the same request is safe. Tool calls are
the opposite: they are never retried automatically; the model sees the failure and decides.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from kama_claude.core.llm.types import LLMError


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4  # attempts after the first
    base_s: float = 1.0
    max_delay_s: float = 30.0
    budget_s: float = 120.0  # total waiting per model call, across its retries

    def delay(self, error: LLMError, retry: int, rng: random.Random | None = None) -> float:
        """Seconds to wait before retry number `retry` (1-based). The server's retry-after
        wins when it gives one (waiting less just earns another 429; the budget still caps
        it). Otherwise exponential backoff with jitter (between half and all of the step),
        so clients hit by the same overload don't come back in lockstep."""
        if error.retry_after_s is not None:
            return error.retry_after_s
        cap = min(self.max_delay_s, self.base_s * 2 ** (retry - 1))
        return (rng or random).uniform(cap / 2, cap)

    def should_retry(self, error: LLMError, retry: int, waited_s: float, delay_s: float) -> bool:
        return error.retryable and retry <= self.max_retries and waited_s + delay_s <= self.budget_s


NO_RETRY = RetryPolicy(max_retries=0)
