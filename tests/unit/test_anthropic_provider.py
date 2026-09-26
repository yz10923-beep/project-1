"""Provider mapping tests: SDK objects in, our types out; no network."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from kama_claude.core.llm.anthropic_provider import AnthropicProvider, to_llm_response
from kama_claude.core.llm.types import LLMError


def beta_message(content: list[dict[str, Any]], stop: str = "tool_use") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": None,
            },
        }
    )


def test_response_mapping_parses_calls_and_keeps_blocks_verbatim() -> None:
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "abc=="},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
    ]
    r = to_llm_response(beta_message(blocks))
    assert r.stop_reason == "tool_use"
    assert r.text == "Let me look."
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [
        ("tu_1", "read_file", {"path": "a.py"})
    ]
    assert r.content[0] == blocks[0]  # thinking signature survives the round trip
    assert r.content[2] == blocks[2]
    assert (r.usage.cache_read_input_tokens, r.usage.cache_creation_input_tokens) == (80, 0)


def test_unknown_stop_reason_maps_to_other() -> None:
    msg = beta_message([{"type": "text", "text": "x"}], stop="end_turn")
    object.__setattr__(msg, "stop_reason", "something_new")
    assert to_llm_response(msg).stop_reason == "other"


def test_request_shape() -> None:
    p = AnthropicProvider(model="claude-opus-5", max_tokens=16000, effort="high", api_key="k")
    req = p.build_request(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert req["cache_control"] == {"type": "ephemeral"}
    assert req["output_config"] == {"effort": "high"}
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]


def test_fallback_not_sent_for_unsupported_models_or_when_disabled() -> None:
    for p in (
        AnthropicProvider(model="claude-haiku-4-5", max_tokens=1, api_key="k"),
        AnthropicProvider(model="claude-opus-5", max_tokens=1, refusal_fallback=False, api_key="k"),
    ):
        req = p.build_request(system="s", messages=[], tools=[])
        assert "fallbacks" not in req and "betas" not in req
        assert "output_config" not in req


@pytest.mark.parametrize(("status", "retryable"), [(400, False), (429, True), (529, True)])
async def test_api_errors_become_llm_errors(status: int, retryable: bool) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"type": "error", "error": {"message": "nope"}})

    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    p = AnthropicProvider(model="claude-opus-5", max_tokens=10, client=client)
    with pytest.raises(LLMError) as exc:
        await p.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert exc.value.retryable is retryable
    assert str(status) in str(exc.value)


async def test_full_loop_over_mocked_http_sends_valid_wire_format(tmp_path: Any) -> None:
    """Real provider + real loop; only the HTTP transport is fake. Checks the JSON we send."""
    import json

    from kama_claude.core.agent.loop import AgentLoop
    from kama_claude.core.tools.builtin import builtin_tools
    from kama_claude.core.tools.registry import ToolRegistry

    (tmp_path / "a.txt").write_text("alpha\n")
    bodies: list[dict[str, Any]] = []
    replies = [
        [
            {"type": "thinking", "thinking": "", "signature": "sig=="},
            {"type": "tool_use", "id": "tu_9", "name": "read_file", "input": {"path": "a.txt"}},
        ],
        [{"type": "text", "text": "It says alpha."}],
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        content = replies[len(bodies) - 1]
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return httpx2.Response(200, json=beta_message(content, stop).model_dump(mode="json"))

    client = anthropic.AsyncAnthropic(
        api_key="k",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    sink_events: list[Any] = []

    class Sink:
        async def emit(self, e: Any) -> None:
            sink_events.append(e)

    async def allow(_: Any) -> bool:
        return True

    loop = AgentLoop(
        provider=AnthropicProvider(model="claude-opus-5", max_tokens=1000, client=client),
        registry=ToolRegistry(builtin_tools()),
        sink=Sink(),
        workspace=tmp_path,
        approver=allow,
    )
    r = await loop.run("read a.txt", "r1")

    assert r.status == "completed" and r.final_text == "It says alpha."
    first, second = bodies
    assert first["model"] == "claude-opus-5" and first["fallbacks"] == "default"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in first["tools"]} == {"bash", "list_dir", "read_file", "write_file"}
    assistant, tool_results = second["messages"][1], second["messages"][2]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig=="}
    assert tool_results["content"][0]["tool_use_id"] == "tu_9"
    assert "alpha" in tool_results["content"][0]["content"]


async def test_missing_credentials_become_non_retryable_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", "/nonexistent")  # no `ant auth` profile on disk
    p = AnthropicProvider(model="claude-opus-5", max_tokens=10)
    with pytest.raises(LLMError) as exc:
        await p.complete(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert not exc.value.retryable and "ANTHROPIC_API_KEY" in str(exc.value)


def test_effort_is_not_sent_to_models_that_reject_it() -> None:
    haiku = AnthropicProvider(model="claude-haiku-4-5", max_tokens=1, effort="high", api_key="k")
    assert haiku.effort is None
    assert "output_config" not in haiku.build_request(system="s", messages=[], tools=[])
    opus = AnthropicProvider(model="claude-opus-5", max_tokens=1, effort="high", api_key="k")
    assert opus.effort == "high"
