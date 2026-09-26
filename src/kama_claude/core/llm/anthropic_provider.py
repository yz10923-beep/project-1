"""Anthropic Messages API provider (raw SDK, manual loop; no tool runner)."""

from __future__ import annotations

import logging
from typing import Any, get_args

import anthropic
from anthropic.types.beta import BetaMessage

from kama_claude.core.llm.types import (
    LLMError,
    LLMResponse,
    Message,
    StopReason,
    TextCallback,
    ToolSpec,
    Usage,
)

# Server-side refusal fallback: on a safety decline the API re-runs the request on
# another model inside the same call. Only some models accept the parameter.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")

# Models that reject `output_config.effort` with a 400.
_NO_EFFORT_MODELS = ("claude-haiku-4-5", "claude-sonnet-4-5")

_KNOWN_STOP_REASONS = set(get_args(StopReason))

logger = logging.getLogger(__name__)


def supports_refusal_fallback(model: str) -> bool:
    return model.startswith(_FALLBACK_MODELS)


def supports_effort(model: str) -> bool:
    return not model.startswith(_NO_EFFORT_MODELS)


def to_llm_response(msg: BetaMessage) -> LLMResponse:
    stop = msg.stop_reason if msg.stop_reason in _KNOWN_STOP_REASONS else "other"
    u = msg.usage
    return LLMResponse(
        stop_reason=stop,  # type: ignore[arg-type]  # narrowed by the set check above
        # exclude_unset keeps exactly the fields the API sent, so the echo is byte-faithful.
        content=[b.to_dict(mode="json") for b in msg.content],
        usage=Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        ),
        model=msg.model,
    )


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        max_tokens: int,
        effort: str | None = None,
        refusal_fallback: bool = True,
        api_key: str | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        if effort and not supports_effort(model):
            # Dropped rather than sent: the API would reject every request with a 400.
            logger.warning("model %s does not support effort; ignoring effort=%s", model, effort)
            effort = None
        self._effort = effort
        self._fallback = refusal_fallback and supports_refusal_fallback(model)
        # With no api_key the SDK resolves credentials itself (env var, `ant auth` profile, ...).
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def effort(self) -> str | None:
        """The effort actually sent (None if unset or unsupported by the model)."""
        return self._effort

    def build_request(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            # Auto-cache the longest stable prefix. Every loop step resends the whole
            # history, so without this input cost grows quadratically with steps.
            "cache_control": {"type": "ephemeral"},
        }
        if self._effort:
            req["output_config"] = {"effort": self._effort}
        if self._fallback:
            req["betas"] = [_FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        on_text: TextCallback | None = None,
    ) -> LLMResponse:
        """Streamed request: text chunks go to `on_text` as they arrive; the full message
        is returned at the end. Streaming also avoids HTTP timeouts on long responses.

        eager_input_streaming is deliberately off: tools only run once the complete
        message is in, so streaming their inputs early would buy nothing."""
        req = self.build_request(system=system, messages=messages, tools=tools)
        # The SDK already retried 408/409/429/5xx and connection errors (max_retries=2),
        # so anything that reaches us here is final for this call.
        try:
            async with self._client.beta.messages.stream(**req) as stream:
                async for event in stream:
                    if event.type == "text" and on_text is not None:
                        await on_text(event.text)
                msg = await stream.get_final_message()
        except anthropic.APIStatusError as e:
            retryable = e.status_code == 429 or e.status_code >= 500
            raise LLMError(f"API error {e.status_code}: {e.message}", retryable=retryable) from e
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            raise LLMError(f"connection error: {e}", retryable=True) from e
        except TypeError as e:
            # The SDK raises a bare TypeError when no credentials resolve at all.
            if "authentication" not in str(e):
                raise
            raise LLMError(
                "no Anthropic credentials: set ANTHROPIC_API_KEY (env or .env) "
                "or run `ant auth login`",
                retryable=False,
            ) from e
        return to_llm_response(msg)
