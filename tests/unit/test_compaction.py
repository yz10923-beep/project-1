"""S6 (3/4): on-demand compaction. When the next request would exceed the budget, the
history is summarized server-side at a step boundary; the conversation continues from
[block] + a resume turn that restates the goal and plan from the run's own records; a
durable event lets replay (and the next run of a session) rebuild exactly that."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from kama_claude.core.agent.history import conversation_problems
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionFailedEvent,
    Event,
    RunFinishedEvent,
)
from kama_claude.core.config import Settings
from kama_claude.core.context import COMPACTION_INSTRUCTIONS
from kama_claude.core.llm.anthropic_provider import (
    COMPACTION_BETA,
    AnthropicProvider,
)
from kama_claude.core.llm.types import LLMError, LLMResponse, Message, ToolCall, Usage
from kama_claude.core.session import SessionStore, read_events
from tests.fakes import (
    CompactingProvider,
    ScriptedProvider,
    compaction_response,
    text_response,
    tool_response,
)

BIG = Usage(input_tokens=20, cache_read_input_tokens=30_000, output_tokens=40)  # > 10K budget
SMALL = Usage(input_tokens=20, cache_read_input_tokens=2_000, output_tokens=40)


def sized(r: LLMResponse, usage: Usage) -> LLMResponse:
    return r.model_copy(update={"usage": usage})


async def _allow(_: ToolCall) -> bool:
    return True


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    kw.setdefault("context_budget", 10_000)
    return Settings(runs_dir=tmp_path / "runs", sessions_dir=tmp_path / "sessions", **kw)


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.txt").write_text("alpha\n")
    return ws


async def _run(
    settings: Settings,
    ws: Path,
    p: ScriptedProvider,
    goal: str = "Count the lines in a.txt, then report.",
    session_id: str | None = None,
) -> tuple[Any, Path, list[Event]]:
    result, run_dir = await run_goal(
        goal,
        settings=settings,
        workspace=ws,
        approver=_allow,
        provider=p,
        mode="auto",
        session_id=session_id,
        sessions=SessionStore(settings.sessions_dir),
    )
    return result, run_dir, read_events(run_dir)


def three_steps(first: Usage = BIG) -> list[LLMResponse | LLMError]:
    return [
        sized(tool_response(("t1", "read_file", {"path": "a.txt"})), first),
        sized(tool_response(("t2", "list_dir", {})), SMALL),
        sized(text_response("1 line."), SMALL),
    ]


async def test_over_budget_the_history_is_compacted_and_continues_from_the_summary(
    tmp_path: Path,
) -> None:
    p = CompactingProvider(three_steps())
    result, run_dir, events = await _run(_settings(tmp_path), _ws(tmp_path), p)
    assert result.status == "completed"
    # what was summarized: the whole history up to the step boundary (a user turn last)
    [asked] = p.compact_requests
    assert asked.messages[-1]["role"] == "user" and len(asked.messages) == 3
    assert p.instructions == [COMPACTION_INSTRUCTIONS]
    # what the model saw next: the block, exactly as returned, then the resume turn
    after = p.requests[1].messages
    assert after[0] == {"role": "assistant", "content": [compaction_response().content[0]]}
    resume = after[1]["content"]
    assert "<goal>\nCount the lines in a.txt, then report.\n</goal>" in resume
    assert "read_output" in resume and conversation_problems(after) == []
    [ev] = [e for e in events if isinstance(e, ContextCompactedEvent)]
    assert (ev.step, ev.messages_replaced) == (2, 3) and ev.tokens_before > 30_060
    assert ev.block == after[0]["content"][0] and ev.resume == resume
    finished = events[-1]
    assert isinstance(finished, RunFinishedEvent) and finished.compactions == 1
    assert finished.usage.input_tokens == 20 * 3 + 900  # the summarizer is billed too
    spans = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text().splitlines()]
    [span] = [s for s in spans if s["name"] == "context.compact"]
    assert span["attrs"]["tokens_before"] == ev.tokens_before


async def test_under_budget_nothing_is_compacted(tmp_path: Path) -> None:
    p = CompactingProvider(three_steps(first=SMALL))
    _, _, events = await _run(_settings(tmp_path), _ws(tmp_path), p)
    assert p.compact_requests == []
    assert not any(isinstance(e, ContextCompactedEvent) for e in events)


async def test_the_resume_turn_restates_the_plan_from_the_runs_own_records(
    tmp_path: Path,
) -> None:
    script = [
        sized(
            tool_response(
                ("c", "task_create", {"tasks": [{"title": "count lines"}, {"title": "report"}]})
            ),
            BIG,
        ),
        sized(
            tool_response(
                (
                    "u",
                    "task_update",
                    {
                        "updates": [
                            {"id": 1, "status": "completed"},
                            {"id": 2, "status": "completed"},
                        ]
                    },
                )
            ),
            SMALL,
        ),
        sized(text_response("done"), SMALL),
    ]
    p = CompactingProvider(script)
    await _run(_settings(tmp_path), _ws(tmp_path), p)
    resume = p.requests[1].messages[1]["content"]
    assert "Your plan, from the run's own records:" in resume and "count lines" in resume


async def test_with_context_off_or_an_unsupported_model_it_never_compacts(
    tmp_path: Path,
) -> None:
    off = CompactingProvider(three_steps())
    await _run(_settings(tmp_path, context=False), _ws(tmp_path), off)
    other = CompactingProvider(three_steps(), model="claude-haiku-4-5")
    await _run(_settings(tmp_path), _ws(tmp_path), other)
    assert off.compact_requests == [] and other.compact_requests == []


async def test_no_summary_means_continue_on_the_full_history_and_wait_to_retry(
    tmp_path: Path,
) -> None:
    cut_off = LLMResponse(stop_reason="max_tokens", content=[], usage=Usage(input_tokens=900))
    script = [sized(tool_response((f"t{i}", "list_dir", {})), BIG) for i in range(5)]
    p = CompactingProvider([*script, sized(text_response("ok"), BIG)], compactions=[cut_off])
    result, _, events = await _run(_settings(tmp_path), _ws(tmp_path), p)
    assert result.status == "completed"
    failed = [e for e in events if isinstance(e, ContextCompactionFailedEvent)]
    done = [e for e in events if isinstance(e, ContextCompactedEvent)]
    assert [e.step for e in failed] == [2] and "max_tokens" in failed[0].reason
    assert [e.step for e in done] == [5]  # waited COMPACT_RETRY_STEPS before trying again
    assert len(p.requests[1].messages) == 3  # step 2 went on with the full history


async def test_a_retryable_compaction_error_is_retried(tmp_path: Path) -> None:
    busy = LLMError("API error 529: compaction_unavailable", retryable=True, kind="overloaded")
    p = CompactingProvider(three_steps(), compactions=[busy, compaction_response()])
    settings = _settings(tmp_path, llm_max_retries=2)
    import kama_claude.core.agent.loop as loop_mod

    async def no_wait(_: float) -> None:
        return None

    loop_mod.asyncio.sleep, orig = no_wait, loop_mod.asyncio.sleep  # type: ignore[assignment]
    try:
        _, _, events = await _run(settings, _ws(tmp_path), p)
    finally:
        loop_mod.asyncio.sleep = orig  # type: ignore[assignment]
    assert len(p.compact_requests) == 2
    assert any(isinstance(e, ContextCompactedEvent) for e in events)


async def test_a_session_continues_from_the_compacted_view(tmp_path: Path) -> None:
    settings, ws = _settings(tmp_path), _ws(tmp_path)
    store = SessionStore(settings.sessions_dir)
    sid = store.create(ws).session_id
    first = CompactingProvider(three_steps())
    await _run(settings, ws, first, session_id=sid)
    history = store.history(sid)
    assert history[0]["content"][0]["type"] == "compaction" and conversation_problems(history) == []
    assert history[:2] == first.requests[1].messages[:2]  # replay = what was sent
    second = CompactingProvider([sized(text_response("still 1 line"), SMALL)])
    await _run(settings, ws, second, goal="And now?", session_id=sid)
    sent = second.requests[0].messages
    assert sent[0]["content"][0]["type"] == "compaction" and sent[-1]["role"] == "user"
    assert conversation_problems(sent) == []


def test_conversation_problems_accepts_only_a_compaction_block_first() -> None:
    ok: list[Message] = [
        {"role": "assistant", "content": [{"type": "compaction", "content": "s"}]},
        {"role": "user", "content": "go on"},
    ]
    assert conversation_problems(ok) == []
    bad: list[Message] = [{"role": "assistant", "content": "hi"}]
    assert conversation_problems(bad) == ["first message is not from the user"]


# ---------------------------------------------------------------- the provider, over HTTP


def _provider(handler: Any) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider(model="claude-opus-5", max_tokens=8000, client=client)


async def test_provider_compacts_and_bills_every_iteration() -> None:
    seen: list[tuple[dict[str, Any], str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((json.loads(request.content), request.headers.get("anthropic-beta", "")))
        return httpx2.Response(
            200,
            json={
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "compaction", "content": "Summary.", "signature": "EuY"}],
                "stop_reason": "compaction",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "iterations": [
                        {"type": "compaction", "input_tokens": 144, "output_tokens": 276}
                    ],
                },
            },
        )

    p = _provider(handler)
    r = await p.compact(
        system="s", messages=[{"role": "user", "content": "hi"}], tools=[], instructions="keep"
    )
    body, beta = seen[0]
    assert body["compaction"] == {"type": "summarize", "instructions": "keep"}
    assert "fallbacks" not in body and beta == COMPACTION_BETA
    assert r.stop_reason == "compaction"
    assert r.content == [{"type": "compaction", "content": "Summary.", "signature": "EuY"}]
    assert (r.usage.input_tokens, r.usage.output_tokens) == (144, 276)


def test_requests_carrying_a_block_send_the_beta_header() -> None:
    p = AnthropicProvider(
        model="claude-opus-5", max_tokens=10, client=anthropic.AsyncAnthropic(api_key="k")
    )
    plain = p.build_request(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert COMPACTION_BETA not in plain.get("betas", [])
    carried = p.build_request(
        system="s",
        messages=[
            {"role": "assistant", "content": [{"type": "compaction", "content": "x"}]},
            {"role": "user", "content": "go on"},
        ],
        tools=[],
    )
    assert COMPACTION_BETA in carried["betas"] and "fallbacks" in carried


@pytest.mark.parametrize("budget", [10_000])
async def test_rows_report_compactions_and_bill_them(tmp_path: Path, budget: int) -> None:
    from evals.harness import RunConfig, load_tasks, run_suite, summarize

    def factory(_: Settings) -> CompactingProvider:
        return CompactingProvider(
            [sized(tool_response(("l", "list_dir", {})), BIG), sized(text_response("x"), SMALL)]
        )

    cfg = RunConfig(
        settings=Settings(model="claude-opus-5", llm_max_retries=0, context_budget=budget),
        reps=1,
        provider_factory=factory,
        results_dir=tmp_path / "results",
    )
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = [json.loads(x) for x in (cfg.variant_dir / "results.jsonl").read_text().splitlines()]
    assert row["context"]["compactions"] == 1 and row["usage"]["input_tokens"] == 40 + 900
    assert "- compaction: compacted in 1/1 trials (1 compactions, 0 failed)" in summarize(
        cfg.variant_dir
    )


async def test_a_summary_still_over_budget_does_not_compact_every_step(tmp_path: Path) -> None:
    """Found by the retry test above: if the request right after a compaction is still
    over the budget, compacting again can't help, and doing it every step would pay for
    a summary per step. The next compaction waits for the context to grow past it."""
    script = [sized(tool_response((f"t{i}", "list_dir", {})), BIG) for i in range(6)]
    p = CompactingProvider([*script, sized(text_response("ok"), BIG)])
    _, _, events = await _run(_settings(tmp_path), _ws(tmp_path), p)
    assert [e.step for e in events if isinstance(e, ContextCompactedEvent)] == [2]
    assert len(p.compact_requests) == 1


async def test_nothing_is_compacted_before_the_first_response(tmp_path: Path) -> None:
    """A goal bigger than the budget: the first request goes out as is (there is no
    history yet to summarize), and compaction waits for a measured request."""
    p = CompactingProvider([sized(text_response("ok"), BIG)])
    await _run(_settings(tmp_path), _ws(tmp_path), p, goal="x" * 60_000)
    assert p.compact_requests == []
