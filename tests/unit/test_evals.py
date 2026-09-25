"""The eval harness, tested with the scripted provider: no API calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from evals.harness import RunConfig, load_tasks, run_suite, selftest, summarize

from kama_claude.core.config import Settings
from kama_claude.core.llm.types import LLMError, LLMResponse
from tests.fakes import ScriptedProvider, text_response, tool_response

FIXED_CALC = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"


def solve_script() -> list[LLMResponse | LLMError]:
    return [
        tool_response(("w", "write_file", {"path": "calc.py", "content": FIXED_CALC})),
        text_response("Fixed add()."),
    ]


def cfg_for(tmp_path: Path, factory: Any, **kw: Any) -> RunConfig:
    return RunConfig(
        settings=Settings(model="fake-model"),
        reps=kw.pop("reps", 1),
        provider_factory=factory,
        results_dir=tmp_path / "results",
        **kw,
    )


def rows(cfg: RunConfig, name: str = "results.jsonl") -> list[dict[str, Any]]:
    path = cfg.variant_dir / name
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_selftest_all_tasks_graders_are_sane() -> None:
    assert selftest(load_tasks()) == []


async def test_scored_row_is_complete(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script()))
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = rows(cfg)
    assert row["grade"] == {"passed": 1.0, "refused": 0.0}
    assert row["status"] == "ok" and row["model"] == "fake-model"
    assert row["meta"]["changed"] == {"calc.py": "modified"}
    assert row["usage"]["input_tokens"] == 20 and row["steps"] == 2 and row["tool_calls"] == 1
    trace = json.loads((cfg.variant_dir / "traces" / "fix-add-bug_rep0.json").read_text())
    assert [t["role"] for t in trace] == [
        "system",
        "user",
        "tool_call",
        "tool_result",
        "assistant",
    ]
    assert not rows(cfg, "errors.jsonl")


async def test_trials_are_isolated_and_graded_on_end_state(tmp_path: Path) -> None:
    # The agent claims success but changes nothing: must fail on end state.
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider([text_response("All fixed!")]), reps=2)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert [r["grade"]["passed"] for r in rows(cfg)] == [0.0, 0.0]


async def test_resume_skips_scored_trials(tmp_path: Path) -> None:
    calls = 0

    def factory(s: Settings) -> ScriptedProvider:
        nonlocal calls
        calls += 1
        return ScriptedProvider(solve_script())

    cfg = cfg_for(tmp_path, factory, reps=2)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert calls == 2 and len(rows(cfg)) == 2


async def test_serving_errors_are_retried_and_kept_out_of_scores(tmp_path: Path) -> None:
    attempts = iter([[LLMError("API error 529", retryable=True)], solve_script()])
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(next(attempts)))
    import evals.harness as h

    orig_sleep = asyncio.sleep
    h.asyncio.sleep = lambda *_: orig_sleep(0)  # type: ignore[assignment]
    try:
        await run_suite(load_tasks(["fix-add-bug"]), cfg)
    finally:
        h.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    [row] = rows(cfg)
    [err] = rows(cfg, "errors.jsonl")
    assert row["attempts"] == 2 and row["grade"]["passed"] == 1.0
    assert err["class"] == "serving_error" and err["attempt"] == 1


async def test_timeout_is_an_error_not_a_zero(tmp_path: Path) -> None:
    class Hang(ScriptedProvider):
        async def complete(self, **_: Any) -> LLMResponse:
            await asyncio.sleep(3600)
            raise AssertionError

    cfg = cfg_for(tmp_path, lambda s: Hang([]), timeout_s=0.2)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert rows(cfg) == []
    assert rows(cfg, "errors.jsonl")[0]["class"] == "timeout"


async def test_wrong_served_model_is_rejected(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script(), model="other-model"))
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert rows(cfg) == []
    assert rows(cfg, "errors.jsonl")[0]["class"] == "model_mismatch"


async def test_reading_the_answer_key_is_flagged(tmp_path: Path) -> None:
    script = [
        tool_response(("b", "bash", {"command": "cat ../../evals/tasks/fix-add-bug/check.py"})),
        *solve_script(),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert rows(cfg)[0]["meta"]["leak_suspect"] is True


async def test_summary_reports_rates_and_errors(tmp_path: Path) -> None:
    scripts = iter([solve_script(), [text_response("nope")], solve_script()])
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(next(scripts)), reps=3, concurrency=1)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    text = summarize(cfg.variant_dir)
    assert "2/3 trials passed" in text
    assert "pass@k (any rep passed): 1/1" in text and "pass^k (every rep passed): 0/1" in text
    assert "| fix-add-bug |" in text
