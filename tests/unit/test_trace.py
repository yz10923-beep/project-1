"""Tracing: span structure, the analysis numbers, and the three instrumented layers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.agent.loop import AgentLoop
from kama_claude.core.agent.runner import TRACE_FILE, run_goal
from kama_claude.core.config import Settings
from kama_claude.core.llm.pricing import cost_usd
from kama_claude.core.llm.types import ToolCall
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.analyze import load_spans, render, summarize, to_chrome
from kama_claude.core.trace.span import Span
from kama_claude.core.trace.tracer import JsonlSpanWriter, Tracer
from tests.fakes import ScriptedProvider, text_response, tool_response


class ListSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def write(self, span: Span) -> None:
        self.spans.append(span)


def by_name(spans: list[Span]) -> dict[str, Span]:
    return {s.name: s for s in spans}


# ---- the tracer


async def test_parents_follow_the_async_structure() -> None:
    sink = ListSink()
    tracer = Tracer("t1", sink)

    async def worker(n: int) -> None:
        with tracer.span(f"task{n}", "tool"):
            await asyncio.sleep(0.01)

    with tracer.span("root", "agent"):
        with tracer.span("child", "llm"):
            await asyncio.sleep(0)
        await asyncio.gather(worker(1), worker(2))  # concurrent tasks

    s = by_name(sink.spans)
    root = s["root"].span_id
    assert s["child"].parent_id == root
    # Concurrent siblings: each task has its own context, so neither nests in the other.
    assert s["task1"].parent_id == root and s["task2"].parent_id == root
    assert s["root"].parent_id is None
    assert [x.name for x in sink.spans][-1] == "root"  # parents end (are written) last


async def test_errors_and_cancellation_are_recorded_and_propagate() -> None:
    sink = ListSink()
    tracer = Tracer("t1", sink)
    with pytest.raises(ValueError), tracer.span("boom", "tool"):
        raise ValueError("bad input")

    async def hang() -> None:
        with tracer.span("slow", "llm"):
            await asyncio.sleep(3600)

    task = asyncio.create_task(hang())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    s = by_name(sink.spans)
    assert (s["boom"].status, s["boom"].error) == ("error", "ValueError: bad input")
    assert s["slow"].status == "cancelled"


async def test_span_from_another_trace_becomes_a_link_not_a_parent() -> None:
    daemon_sink, run_sink = ListSink(), ListSink()
    with Tracer("daemon", daemon_sink).span("rpc run.start", "ipc") as rpc:
        with Tracer("run-1", run_sink).span("run", "agent"):
            pass
    [run] = run_sink.spans
    assert run.parent_id is None
    assert run.attrs["linked_span"] == f"daemon/{rpc.span_id}"


def test_writer_roundtrip_sorted_by_start(tmp_path: Path) -> None:
    tracer = Tracer("t1", JsonlSpanWriter(tmp_path / TRACE_FILE))
    tracer.record("later", "ipc", start_ns=2_000, duration_ns=5, attrs={"method": "m"})
    tracer.record("earlier", "ipc", start_ns=1_000, duration_ns=5)
    spans = load_spans(tmp_path / TRACE_FILE)
    assert [s.name for s in spans] == ["earlier", "later"]
    assert spans[1].attrs == {"method": "m"}


def test_pricing_uses_the_longest_matching_model_id() -> None:
    usage = {"input_tokens": 1_000_000}
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0)  # not Opus 5's $5
    assert cost_usd("claude-opus-5", usage) == pytest.approx(5.0)
    assert cost_usd("fake-model", usage) is None  # unknown is None, never a silent $0
    cached = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert cost_usd("claude-opus-5", cached) == pytest.approx(5.0 * 0.1 + 5.0 * 1.25)


# ---- the analysis, on hand-made spans with exact numbers

MS = 1_000_000


def mk(name: str, kind: Any, start_ms: int, dur_ms: int, parent: str | None, **attrs: Any) -> Span:
    return Span(
        trace_id="run-x",
        span_id=name.replace(" ", "_"),
        parent_id=parent,
        name=name,
        kind=kind,
        start_ns=start_ms * MS,
        duration_ns=dur_ms * MS,
        attrs=attrs,
    )


SYNTHETIC = [
    mk("run", "agent", 0, 10_000, None, status="completed", steps=2, model="claude-opus-5"),
    mk("step 1", "agent", 0, 9_000, "run", step=1),
    mk(
        "llm.call",
        "llm",
        0,
        6_000,
        "step_1",
        step=1,
        ttft_ms=1_000,
        output_tokens=500,
        input_tokens=100,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=100,
        cost_usd=0.0135,
    ),
    mk("tool bash", "tool", 6_000, 3_000, "step_1"),
    mk("tool.approval", "tool", 6_000, 1_000, "tool_bash"),
    mk("tool.exec", "tool", 7_000, 2_000, "tool_bash"),
    mk(
        "bus.subscribe",
        "bus",
        0,
        10_000,
        None,
        client="cli",
        ended="finished",
        replayed=0,
        live_events=9,
        deltas=4,
        lag_mean_ms=0.2,
        lag_max_ms=1.5,
    ),
    mk("rpc run.start", "ipc", 0, 3, None, method="run.start"),
]


def test_summary_says_where_time_and_tokens_went() -> None:
    s = summarize(SYNTHETIC)
    assert (s.wall_ms, s.llm_ms, s.tool_ms, s.approval_ms) == (10_000, 6_000, 2_000, 1_000)
    assert s.other_ms == 1_000  # loop overhead = wall minus everything measured
    assert s.cost_usd == pytest.approx(0.0135)
    assert s.cache_hit_ratio == pytest.approx(0.8)  # 800 of 1000 prompt tokens from cache
    [call] = s.calls
    assert call.tokens_per_s == pytest.approx(100.0)  # 500 tokens over 5s of generation
    assert s.slowest[0].name == "llm.call"
    assert s.ipc["run.start"]["count"] == 1


def test_render_and_chrome_export() -> None:
    text = render(SYNTHETIC, width=20)
    assert "model        6.0s   60%" in text
    assert "approval     1.0s   10%" in text
    assert "cache hit 80%" in text
    assert "500 out tok at 100 tok/s" in text
    chrome = to_chrome(SYNTHETIC)
    xs = [e for e in chrome["traceEvents"] if e["ph"] == "X"]
    assert len(xs) == len(SYNTHETIC)
    llm = next(e for e in xs if e["name"] == "llm.call")
    assert (llm["ts"], llm["dur"]) == (0, 6_000_000)  # microseconds
    json.dumps(chrome)  # serializable


# ---- the agent layer, through a real loop


async def test_loop_writes_a_span_tree(tmp_path: Path) -> None:
    sink = ListSink()

    async def yes(_: ToolCall) -> bool:
        return True

    loop = AgentLoop(
        provider=ScriptedProvider(
            [
                tool_response(("w", "write_file", {"path": "f.txt", "content": "x"})),
                text_response("done"),
            ]
        ),
        registry=ToolRegistry(builtin_tools()),
        sink=_NullEvents(),
        workspace=tmp_path,
        approver=yes,
        tracer=Tracer("r1", sink),
    )
    await loop.run("go", "r1")
    spans = sink.spans
    ids = {s.span_id: s for s in spans}

    def path(s: Span) -> str:
        parts = [s.name]
        while s.parent_id:
            s = ids[s.parent_id]
            parts.append(s.name)
        return " > ".join(reversed(parts))

    paths = {path(s) for s in spans}
    assert {
        "run > step 1 > llm.call",
        "run > step 1 > tool write_file > tool.approval",
        "run > step 1 > tool write_file > tool.exec",
        "run > step 2 > llm.call",
    } <= paths
    llm = next(s for s in spans if s.name == "llm.call")
    assert llm.attrs["input_tokens"] == 10 and llm.attrs["stop_reason"] == "tool_use"
    assert "cost_usd" not in llm.attrs  # fake model has no price: absent, not $0
    run = next(s for s in spans if s.name == "run")
    assert run.attrs["status"] == "completed" and run.attrs["steps"] == 2
    assert "cost unknown" in render(spans)


async def test_run_goal_writes_trace_next_to_events(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    async def yes(_: ToolCall) -> bool:
        return True

    result, run_dir = await run_goal(
        "go",
        settings=Settings(runs_dir=tmp_path / "runs"),
        workspace=ws,
        approver=yes,
        provider=ScriptedProvider([text_response("hi")]),
    )
    spans = load_spans(run_dir / TRACE_FILE)
    assert {s.trace_id for s in spans} == {result.run_id}
    assert summarize(spans).status == "completed"


class _NullEvents:
    async def emit(self, event: Any) -> None:
        pass
