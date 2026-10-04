"""Anthropic Messages API provider (raw SDK, manual loop; no tool runner)."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, get_args

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage

from kama_claude.core.llm.types import (
    RETRYABLE_KINDS,
    LLMError,
    LLMErrorKind,
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


def make_client(api_key: str | None) -> anthropic.AsyncAnthropic:
    """An SDK client without its built-in retries: the agent loop owns retrying (S5)."""
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)


_TYPE_KIND: dict[str, LLMErrorKind] = {
    "rate_limit_error": "rate_limit",
    "overloaded_error": "overloaded",
    "api_error": "server",
    "invalid_request_error": "invalid_request",
    "authentication_error": "auth",
    "permission_error": "permission",
    "not_found_error": "not_found",
    "request_too_large": "too_large",
    "billing_error": "billing",
    "timeout_error": "server",
}


def status_kind(status: int, error_type: str | None) -> LLMErrorKind:
    """The error's kind: by the API's error type when present (a mid-stream error event
    arrives on a 200 response), else by HTTP status."""
    if error_type in _TYPE_KIND:
        return _TYPE_KIND[error_type]
    if status == 429:
        return "rate_limit"
    if status == 529:
        return "overloaded"
    if status in (408, 409) or status >= 500:
        return "server"
    by_status: dict[int, LLMErrorKind] = {
        400: "invalid_request",
        401: "auth",
        402: "billing",
        403: "permission",
        404: "not_found",
        413: "too_large",
        422: "invalid_request",
    }
    return by_status.get(status, "unknown")


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds the server asked us to wait: retry-after-ms, then retry-after (seconds)."""
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw) / scale)
        except ValueError:
            continue
    return None


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
        self._client = client or make_client(api_key)

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

    async def count_tokens(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int:
        """The exact input size of a request (S6: the context meter asks near the budget).
        Free, but a round trip: only for decisions the estimate can't make."""
        try:
            res = await self._client.beta.messages.count_tokens(
                model=self._model,
                system=system,
                messages=messages,  # type: ignore[arg-type]  # our Message is the wire shape
                tools=tools,  # type: ignore[arg-type]
            )
        except anthropic.APIError as e:
            raise LLMError(f"token count failed: {e}", retryable=False) from e
        return res.input_tokens

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
        # The SDK's own retries are off (max_retries=0, see make_client): the agent loop
        # retries, so every attempt is visible as an llm.retry event and in the trace.
        t0 = time.perf_counter()
        ttft_ms: int | None = None
        try:
            async with self._client.beta.messages.stream(**req) as stream:
                async for event in stream:
                    # message_start arrives before generation; the first content delta
                    # (text, thinking or tool input) is the first token.
                    if ttft_ms is None and event.type == "content_block_delta":
                        ttft_ms = int((time.perf_counter() - t0) * 1000)
                    if event.type == "text" and on_text is not None:
                        await on_text(event.text)
                msg = await stream.get_final_message()
        except anthropic.APIStatusError as e:
            kind = status_kind(e.status_code, e.type)
            if kind == "invalid_request" and "prompt is too long" in e.message.lower():
                kind = "context_overflow"
            if kind in RETRYABLE_KINDS and ttft_ms is not None and e.status_code < 300:
                kind = "stream_interrupted" if kind != "overloaded" else kind
            raise LLMError(
                f"API error {e.status_code if e.status_code >= 300 else e.type}: {e.message}",
                retryable=kind in RETRYABLE_KINDS,
                kind=kind,
                status=e.status_code,
                retry_after_s=retry_after(e.response.headers),
            ) from e
        except anthropic.APIConnectionError as e:  # includes APITimeoutError
            kind = "stream_interrupted" if ttft_ms is not None else "connection"
            raise LLMError(f"connection error: {e}", retryable=True, kind=kind) from e
        except httpx2.TransportError as e:  # the stream broke mid-response
            raise LLMError(
                f"stream interrupted: {type(e).__name__}: {e}",
                retryable=True,
                kind="stream_interrupted" if ttft_ms is not None else "connection",
            ) from e
        except TypeError as e:
            # The SDK raises a bare TypeError when no credentials resolve at all.
            if "authentication" not in str(e):
                raise
            raise LLMError(
                "no Anthropic credentials: set ANTHROPIC_API_KEY (env or .env) "
                "or run `ant auth login`",
                retryable=False,
                kind="auth",
            ) from e
        return to_llm_response(msg).model_copy(update={"ttft_ms": ttft_ms})
