from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.agent.loop import DENIED_MESSAGE, AgentLoop
from kama_claude.core.agent.runner import run_goal
from kama_claude.core.bus.events import EVENT_ADAPTER, Event
from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMError, LLMResponse, ToolCall
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.registry import ToolRegistry
from tests.fakes import ScriptedProvider, text_response, tool_response


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events if e.type != "llm.delta"]


async def allow(_: ToolCall) -> bool:
    return True


async def deny(_: ToolCall) -> bool:
    return False


def make_loop(
    provider: ScriptedProvider, ws: Path, approver: Any = allow, max_steps: int = 10
) -> tuple[AgentLoop, ListSink]:
    sink = ListSink()
    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        sink=sink,
        workspace=ws,
        approver=approver,
        max_steps=max_steps,
    )
    return loop, sink


async def test_immediate_answer_completes(tmp_path: Path) -> None:
    p = ScriptedProvider([text_response("done")])
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("say done", "r1")
    assert (r.status, r.final_text, r.steps) == ("completed", "done", 1)
    assert sink.types() == ["run.started", "llm.response", "run.finished"]
    assert p.requests[0].messages == [{"role": "user", "content": "say done"}]


async def test_tool_roundtrip_feeds_result_back_with_matching_id(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("secret-value\n")
    first = tool_response(("tu_1", "read_file", {"path": "x.txt"}), text="Reading.")
    # An opaque block (e.g. thinking) must be echoed back untouched.
    first.content.insert(0, {"type": "thinking", "thinking": "", "signature": "sig=="})
    p = ScriptedProvider([first, text_response("it says secret-value")])
    loop, _ = make_loop(p, tmp_path)
    r = await loop.run("what is in x.txt", "r1")

    assert r.status == "completed"
    second = p.requests[1].messages
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"] == first.content  # verbatim echo, thinking block included
    [result] = second[2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1"
    assert "secret-value" in result["content"] and "is_error" not in result


async def test_parallel_calls_return_all_results_in_one_message_in_order(
    tmp_path: Path,
) -> None:
    p = ScriptedProvider(
        [
            tool_response(
                ("a", "list_dir", {}),
                ("b", "read_file", {"path": "missing"}),
                ("c", "no_such_tool", {}),
            ),
            text_response("ok"),
        ]
    )
    loop, _ = make_loop(p, tmp_path)
    await loop.run("go", "r1")
    results = p.requests[1].messages[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["a", "b", "c"]
    assert [b.get("is_error", False) for b in results] == [False, True, True]


async def test_denied_tool_is_not_executed_and_model_is_told(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(("w", "write_file", {"path": "f.txt", "content": "x"})),
            text_response("ok, stopping"),
        ]
    )
    loop, sink = make_loop(p, tmp_path, approver=deny)
    r = await loop.run("write", "r1")
    assert r.status == "completed"
    assert not (tmp_path / "f.txt").exists()
    [result] = p.requests[1].messages[2]["content"]
    assert result["is_error"] is True and result["content"] == DENIED_MESSAGE
    finished = [e for e in sink.events if e.type == "tool.finished"]
    assert finished[0].denied  # type: ignore[union-attr]


async def test_read_only_tools_skip_approval(tmp_path: Path) -> None:
    p = ScriptedProvider([tool_response(("l", "list_dir", {})), text_response("ok")])
    loop, _ = make_loop(p, tmp_path, approver=deny)
    await loop.run("list", "r1")
    [result] = p.requests[1].messages[2]["content"]
    assert "is_error" not in result


async def test_max_steps_stops_a_looping_model(tmp_path: Path) -> None:
    script: list[LLMResponse | LLMError] = [
        tool_response((f"t{i}", "list_dir", {})) for i in range(3)
    ]
    p = ScriptedProvider(script)
    loop, _ = make_loop(p, tmp_path, max_steps=3)
    r = await loop.run("loop forever", "r1")
    assert (r.status, r.steps) == ("max_steps", 3)


@pytest.mark.parametrize(
    ("stop", "status"),
    [("max_tokens", "truncated"), ("refusal", "refused"), ("other", "error")],
)
async def test_terminal_stop_reasons(tmp_path: Path, stop: Any, status: str) -> None:
    p = ScriptedProvider([text_response("partial", stop=stop)])
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("go", "r1")
    assert r.status == status and r.error
    assert sink.types()[-1] == "run.finished"


async def test_tool_use_without_calls_is_an_error(tmp_path: Path) -> None:
    p = ScriptedProvider([text_response("hmm", stop="tool_use")])
    loop, _ = make_loop(p, tmp_path)
    assert (await loop.run("go", "r1")).status == "error"


async def test_llm_error_finishes_run_with_error(tmp_path: Path) -> None:
    p = ScriptedProvider([LLMError("API error 529: overloaded", retryable=True)])
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("go", "r1")
    assert r.status == "error" and "529" in (r.error or "")
    assert sink.types() == ["run.started", "run.finished"]


async def test_cancellation_still_emits_run_finished(tmp_path: Path) -> None:
    class Hanging(ScriptedProvider):
        async def complete(self, **_: Any) -> LLMResponse:
            await asyncio.sleep(3600)
            raise AssertionError

    loop, sink = make_loop(Hanging([]), tmp_path)
    task = asyncio.create_task(loop.run("go", "r1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = sink.events[-1]
    assert last.type == "run.finished" and last.status == "cancelled"  # type: ignore[union-attr]


async def test_usage_is_summed_across_steps(tmp_path: Path) -> None:
    p = ScriptedProvider([tool_response(("l", "list_dir", {})), text_response("ok")])
    loop, _ = make_loop(p, tmp_path)
    r = await loop.run("go", "r1")
    assert (r.usage.input_tokens, r.usage.output_tokens) == (20, 10)


async def test_events_jsonl_is_complete_and_ordered(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(("w", "write_file", {"path": "hello.txt", "content": "hi"})),
            text_response("wrote hello.txt"),
        ]
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    result, run_dir = await run_goal(
        "write hello",
        settings=Settings(runs_dir=tmp_path / "runs"),
        workspace=ws,
        approver=allow,
        provider=p,
    )
    assert result.status == "completed"
    assert run_dir.parent == tmp_path / "runs"  # outside the workspace
    tmp_path = ws
    events = [
        EVENT_ADAPTER.validate_json(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert [e.type for e in events] == [
        "run.started",
        "llm.response",
        "tool.started",
        "tool.approval_requested",
        "tool.approval_resolved",
        "tool.finished",
        "llm.response",
        "run.finished",
    ]  # llm.delta events are streamed but never written to disk
    assert [e.seq for e in events] == list(range(len(events)))  # type: ignore[union-attr]
    assert {e.run_id for e in events} == {result.run_id}  # type: ignore[union-attr]
    assert (tmp_path / "hello.txt").read_text() == "hi"


async def test_unexpected_provider_exception_still_finishes_run(tmp_path: Path) -> None:
    class Broken(ScriptedProvider):
        async def complete(self, **_: Any) -> LLMResponse:
            raise KeyError("bug")

    loop, sink = make_loop(Broken([]), tmp_path)
    r = await loop.run("go", "r1")
    assert r.status == "error" and "KeyError" in (r.error or "")
    assert sink.types() == ["run.started", "run.finished"]


async def test_approval_wait_is_timed_separately_from_execution(tmp_path: Path) -> None:
    async def slow_yes(_: ToolCall) -> bool:
        await asyncio.sleep(0.3)
        return True

    p = ScriptedProvider(
        [
            tool_response(
                ("w", "write_file", {"path": "f.txt", "content": "x"}),
                ("l", "list_dir", {}),
            ),
            text_response("ok"),
        ]
    )
    loop, sink = make_loop(p, tmp_path, approver=slow_yes)
    await loop.run("go", "r1")
    write, ls = [e for e in sink.events if e.type == "tool.finished"]
    assert write.approval_ms >= 250  # type: ignore[union-attr]
    assert write.duration_ms < 250  # type: ignore[union-attr]  # execution only
    assert ls.approval_ms == 0  # type: ignore[union-attr]  # read-only: never asked


async def test_llm_error_retryability_reaches_run_result(tmp_path: Path) -> None:
    p = ScriptedProvider([LLMError("API error 400: bad param", retryable=False)])
    loop, sink = make_loop(p, tmp_path)
    r = await loop.run("go", "r1")
    assert r.retryable is False
    assert sink.events[-1].retryable is False  # type: ignore[union-attr]


async def test_text_is_streamed_as_deltas_for_the_right_step(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [tool_response(("l", "list_dir", {}), text="Looking around."), text_response("All done.")]
    )
    loop, sink = make_loop(p, tmp_path)
    await loop.run("go", "r1")
    deltas = [e for e in sink.events if e.type == "llm.delta"]
    by_step: dict[int, str] = {}
    for d in deltas:
        by_step[d.step] = by_step.get(d.step, "") + d.text  # type: ignore[union-attr]
    assert {k: v.strip() for k, v in by_step.items()} == {1: "Looking around.", 2: "All done."}


async def test_approval_events_record_who_decided(tmp_path: Path) -> None:
    from kama_claude.core.agent.loop import ApprovalDecision

    async def timed_out(_: ToolCall) -> ApprovalDecision:
        return ApprovalDecision(False, "timeout")

    p = ScriptedProvider(
        [tool_response(("w", "write_file", {"path": "f", "content": "x"})), text_response("ok")]
    )
    loop, sink = make_loop(p, tmp_path, approver=timed_out)
    await loop.run("go", "r1")
    [resolved] = [e for e in sink.events if e.type == "tool.approval_resolved"]
    assert (resolved.approved, resolved.by) == (False, "timeout")  # type: ignore[union-attr]
    assert not (tmp_path / "f").exists()
