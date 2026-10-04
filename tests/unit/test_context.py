"""S6 (2/4): results cut at the cap are kept whole and readable, and request sizes are
measured. With KAMA_CONTEXT=false nothing the model sees changes (the S5 agent)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest

from kama_claude.core.agent.runner import run_goal
from kama_claude.core.bus.events import Event, RunFinishedEvent, ToolFinishedEvent
from kama_claude.core.config import Settings
from kama_claude.core.context import ContextMeter, estimate_tokens
from kama_claude.core.llm.anthropic_provider import AnthropicProvider
from kama_claude.core.llm.types import Message, ToolCall, ToolSpec, Usage
from kama_claude.core.outputs import OutputStore, cut
from kama_claude.core.policy.engine import Policy
from kama_claude.core.session import SessionStore, read_events
from kama_claude.core.tools.base import ToolContext
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.output_tools import output_tools
from kama_claude.core.tools.registry import ToolRegistry, truncate_middle
from tests.fakes import ScriptedProvider, text_response, tool_response

LINES = "".join(f"line {i:05d} " + "x" * 40 + "\n" for i in range(1, 2001))  # ~100K chars


# ---------------------------------------------------------------- the cut and the store


def test_cut_keeps_whole_lines_and_names_whats_missing() -> None:
    shown, info = cut(LINES, 30_000, "t1")
    head, tail = shown.split("[... ", 1)[0], shown.rsplit("...]\n", 1)[1]
    assert head.endswith("x" * 40 + "\n") and tail.startswith("line ")  # whole lines
    assert all(line.startswith("line ") for line in (head + "\n" + tail).split("\n") if line)
    assert info.original_chars == len(LINES) and info.total_lines == 2000
    assert info.kept_chars == len(head) + len(tail) <= 30_000
    first_missing = head.count("\n") + 1
    assert f"(lines {first_missing}-" in shown and "of 2000)" in shown
    assert 'read_output(id="t1", offset=<line>, limit=<lines>)' in shown


def test_cut_of_one_long_line_says_so() -> None:
    shown, info = cut("y" * 50_000, 10_000, "t2")
    assert "(part of line 1)" in shown and info.total_lines == 1


def test_store_finds_this_run_then_earlier_runs_and_refuses_paths(tmp_path: Path) -> None:
    old = OutputStore(tmp_path / "r1")
    old.save("toolu_old", "from run 1")
    store = OutputStore(tmp_path / "r2", [tmp_path / "r1"])
    store.save("toolu_new", "from run 2")
    assert store.find("toolu_new") == tmp_path / "r2" / "toolu_new.txt"
    assert store.find("toolu_old") == tmp_path / "r1" / "toolu_old.txt"
    assert store.find("../r1/toolu_old") is None and store.find("") is None
    with pytest.raises(ValueError):
        store.save("../escape", "x")


# ---------------------------------------------------------------- the registry


async def _bash(ctx: ToolContext, output_id: str | None = "t1") -> Any:
    reg = ToolRegistry(builtin_tools())
    return await reg.execute("bash", {"command": "seq 1 40000"}, ctx, output_id=output_id)


async def test_over_the_cap_the_whole_output_is_saved_and_the_cut_recorded(
    tmp_path: Path,
) -> None:
    store = OutputStore(tmp_path / "out")
    r = await _bash(ToolContext(workspace=tmp_path, outputs=store, max_result_chars=10_000))
    full = (tmp_path / "out" / "t1.txt").read_text()
    assert full.splitlines()[1:3] == ["1", "2"] and full.rstrip().endswith("40000")
    assert r.cut is not None and r.cut.original_chars == len(full)
    assert 'saved as output "t1"' in r.content and len(r.content) < 10_000 + 600


async def test_context_off_is_the_s1_cut_byte_for_byte(tmp_path: Path) -> None:
    r = await _bash(ToolContext(workspace=tmp_path))  # no store: KAMA_CONTEXT=false
    full = "exit_code: 0\n" + "\n".join(str(i) for i in range(1, 40001))
    assert r.content == truncate_middle(full) and r.cut is None


async def test_under_the_cap_nothing_changes(tmp_path: Path) -> None:
    store = OutputStore(tmp_path / "out")
    reg = ToolRegistry(builtin_tools())
    ctx = ToolContext(workspace=tmp_path, outputs=store)
    r = await reg.execute("bash", {"command": "echo hi"}, ctx, output_id="t1")
    assert (r.content, r.cut) == ("exit_code: 0\nhi", None)
    assert not (tmp_path / "out").exists()


# ---------------------------------------------------------------- read_output, read_file


async def test_read_output_pages_through_a_saved_output(tmp_path: Path) -> None:
    store = OutputStore(tmp_path / "out")
    store.save("t9", LINES + "z" * 5000 + "\n")
    reg = ToolRegistry(output_tools())
    ctx = ToolContext(workspace=tmp_path, outputs=store, max_line_chars=2000)
    r = await reg.execute("read_output", {"id": "t9", "offset": 1500, "limit": 3}, ctx)
    assert r.content.splitlines()[0] == "  1500\tline 01500 " + "x" * 40
    assert r.content.endswith("(showing lines 1500-1502 of 2001)")
    last = await reg.execute("read_output", {"id": "t9", "offset": 2001}, ctx)
    assert last.content.endswith("[... line cut: 5000 characters]")
    missing = await reg.execute("read_output", {"id": "nope"}, ctx)
    assert (missing.is_error, missing.error_kind) == (True, "not_found")
    past = await reg.execute("read_output", {"id": "t9", "offset": 9999}, ctx)
    assert past.error_kind == "invalid_input"


async def test_read_file_cuts_long_lines_only_with_context_on(tmp_path: Path) -> None:
    (tmp_path / "min.js").write_text("a" * 9000 + "\nshort\n")
    reg = ToolRegistry(builtin_tools())
    on = await reg.execute(
        "read_file", {"path": "min.js"}, ToolContext(workspace=tmp_path, max_line_chars=2000)
    )
    off = await reg.execute("read_file", {"path": "min.js"}, ToolContext(workspace=tmp_path))
    assert "[... line cut: 9000 characters]" in on.content and "short" in on.content
    assert "a" * 9000 in off.content


def test_the_policy_allows_read_output_in_every_mode(tmp_path: Path) -> None:
    for mode in ("default", "auto", "read-only"):
        p = Policy.load(tmp_path, mode=mode, user_file=tmp_path / "none.toml")  # type: ignore[arg-type]
        assert p.check("read_output", {"id": "t1"}).action == "allow"


# ---------------------------------------------------------------- end to end


async def _allow(_: ToolCall) -> bool:
    return True


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(runs_dir=tmp_path / "runs", sessions_dir=tmp_path / "sessions", **kw)


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


async def _run(
    settings: Settings, ws: Path, p: ScriptedProvider, session_id: str | None = None
) -> tuple[Any, Path, list[Event]]:
    result, run_dir = await run_goal(
        "look at the numbers",
        settings=settings,
        workspace=ws,
        approver=_allow,
        provider=p,
        mode="auto",
        session_id=session_id,
        sessions=SessionStore(settings.sessions_dir),
    )
    return result, run_dir, read_events(run_dir)


async def test_a_cut_result_is_recorded_saved_and_readable_by_the_model(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [
            tool_response(("t1", "bash", {"command": "seq 1 40000"})),
            tool_response(("t2", "read_output", {"id": "t1", "offset": 20000, "limit": 2})),
            text_response("20000 is in the middle."),
        ]
    )
    result, run_dir, events = await _run(_settings(tmp_path), _ws(tmp_path), p)
    assert result.status == "completed"
    seq_done = next(e for e in events if isinstance(e, ToolFinishedEvent) and e.tool_use_id == "t1")
    assert seq_done.cut is not None and seq_done.cut.output_id == "t1"
    sent = p.requests[1].messages[-1]["content"][0]["content"]
    assert sent == seq_done.output  # the event holds exactly what the model saw
    assert (run_dir / "outputs" / "t1.txt").is_file()
    read = next(e for e in events if isinstance(e, ToolFinishedEvent) and e.tool_use_id == "t2")
    assert read.output.splitlines()[:2] == [" 20000\t19999", " 20001\t20000"]
    assert "read_output" in {t["name"] for t in p.requests[0].tools}


async def test_with_context_off_the_model_gets_exactly_the_s5_request(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [tool_response(("t1", "bash", {"command": "seq 1 40000"})), text_response("ok")]
    )
    _, run_dir, events = await _run(_settings(tmp_path, context=False), _ws(tmp_path), p)
    assert "read_output" not in {t["name"] for t in p.requests[0].tools}
    full = "exit_code: 0\n" + "\n".join(str(i) for i in range(1, 40001))
    assert p.requests[1].messages[-1]["content"][0]["content"] == truncate_middle(full)
    assert not (run_dir / "outputs").exists()
    assert all(e.cut is None for e in events if isinstance(e, ToolFinishedEvent))


async def test_a_later_run_in_the_session_reads_an_earlier_runs_output(tmp_path: Path) -> None:
    settings, ws = _settings(tmp_path), _ws(tmp_path)
    sid = SessionStore(settings.sessions_dir).create(ws).session_id
    first = ScriptedProvider(
        [tool_response(("t1", "bash", {"command": "seq 1 40000"})), text_response("long")]
    )
    await _run(settings, ws, first, session_id=sid)
    second = ScriptedProvider(
        [
            tool_response(("t5", "read_output", {"id": "t1", "offset": 2, "limit": 1})),
            text_response("1"),
        ]
    )
    _, _, events = await _run(settings, ws, second, session_id=sid)
    read = next(e for e in events if isinstance(e, ToolFinishedEvent) and e.tool_use_id == "t5")
    assert (read.is_error, read.output.splitlines()[0]) == (False, "     2\t1")


async def test_runs_record_request_sizes_and_the_estimate_beside_them(tmp_path: Path) -> None:
    usage = Usage(input_tokens=50, cache_read_input_tokens=3000, cache_creation_input_tokens=400)
    calls = [tool_response(("t1", "bash", {"command": "echo hi"})), text_response("hi")]
    for c in calls:
        c.usage = usage.model_copy(update={"output_tokens": 30})
    _, run_dir, events = await _run(_settings(tmp_path), _ws(tmp_path), ScriptedProvider(calls))
    finished = events[-1]
    assert isinstance(finished, RunFinishedEvent) and finished.context_peak == 3450
    spans = [json.loads(x) for x in (run_dir / "trace.jsonl").read_text().splitlines()]
    llm = [s["attrs"] for s in spans if s["name"] == "llm.call"]
    assert [a["context_tokens"] for a in llm] == [3450, 3450]
    assert llm[0]["context_estimate"] > 0 and llm[1]["context_estimate"] >= 3450 + 30


# ---------------------------------------------------------------- the meter


class Counter:
    def __init__(self, n: int | Exception) -> None:
        self.n, self.calls = n, 0

    async def count_tokens(
        self, *, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int:
        self.calls += 1
        if isinstance(self.n, Exception):
            raise self.n
        return self.n


async def test_meter_is_exact_up_to_the_last_request_and_estimates_the_rest() -> None:
    m = ContextMeter(budget=10_000)
    msgs: list[Message] = [{"role": "user", "content": "go"}]
    first = m.estimate("sys", [], msgs)
    assert first == estimate_tokens("sys") + estimate_tokens([]) + estimate_tokens(msgs)
    m.observe(Usage(input_tokens=5, cache_read_input_tokens=995, output_tokens=40), len(msgs))
    msgs += [{"role": "assistant", "content": "reply"}, {"role": "user", "content": "x" * 300}]
    assert m.estimate("sys", [], msgs) == 1000 + 40 + estimate_tokens(msgs[2:])
    assert (m.peak, m.mean) == (1000, 1000)


async def test_meter_counts_exactly_only_near_the_budget() -> None:
    counter = Counter(9_321)
    m = ContextMeter(budget=10_000, counter=counter)
    small: list[Message] = [{"role": "user", "content": "hi"}]
    assert await m.measure("s", [], small) == (m.estimate("s", [], small), "estimate")
    big: list[Message] = [{"role": "user", "content": "w" * 27_000}]  # ~9000 tokens estimated
    assert await m.measure("s", [], big) == (9_321, "count") and counter.calls == 1
    broken = ContextMeter(budget=10_000, counter=Counter(RuntimeError("down")))
    assert await broken.measure("s", [], big) == (broken.estimate("s", [], big), "estimate")


async def test_provider_counts_tokens_over_http() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages/count_tokens"
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json={"input_tokens": 4242})

    client = anthropic.AsyncAnthropic(
        api_key="k",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    p = AnthropicProvider(model="claude-opus-5", max_tokens=10, client=client)
    n = await p.count_tokens(system="s", messages=[{"role": "user", "content": "hi"}], tools=[])
    assert n == 4242 and seen[0]["model"] == "claude-opus-5" and seen[0]["system"] == "s"
