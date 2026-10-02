"""The eval harness, tested with the scripted provider: no API calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from evals.harness import RunConfig, SuiteAborted, load_tasks, run_suite, selftest, summarize

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


# If this changes, the log the agent sees changed, and scores from before and after are
# not comparable. Update it deliberately: re-run make_answers.py, selftest, re-approve.
LOG_TRIAGE_SHA = "2bd5a9882a9e0a716948a8b984418cc33b2717b602ec797d7695fe1c65ac52b5"


def test_log_triage_fixture_is_frozen() -> None:
    from evals.harness import TASKS_DIR, load_task_module

    setup = load_task_module(TASKS_DIR / "log-error-triage", "setup")
    assert setup.log_sha256() == LOG_TRIAGE_SHA


async def test_setup_hook_generates_inputs_for_each_trial(tmp_path: Path) -> None:
    answer = json.loads((Path("evals/tasks/log-error-triage/oracle/answer.json")).read_text())
    script: list[LLMResponse | LLMError] = [
        tool_response(("g", "bash", {"command": "grep -c 'level=ERROR' logs/app.log"})),
        tool_response(("w", "write_file", {"path": "answer.json", "content": json.dumps(answer)})),
        text_response("Done."),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    await run_suite(load_tasks(["log-error-triage"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]
    assert row["meta"]["changed"] == {"answer.json": "added"}  # generated log is not a change


async def test_request_error_is_not_retried_and_aborts_the_suite(tmp_path: Path) -> None:
    calls = 0

    def factory(s: Settings) -> ScriptedProvider:
        nonlocal calls
        calls += 1
        return ScriptedProvider([LLMError("API error 400: unsupported param", retryable=False)])

    cfg = cfg_for(tmp_path, factory, reps=3, concurrency=1)
    with pytest.raises(SuiteAborted, match="unsupported param"):
        await run_suite(load_tasks(["fix-add-bug", "vwap-cli"]), cfg)
    assert calls == 1  # no retry, and no further trials started
    [err] = rows(cfg, "errors.jsonl")
    assert err["class"] == "request_error"


def planned_solve_script() -> list[LLMResponse | LLMError]:
    return [
        tool_response(
            ("c", "task_create", {"tasks": [{"title": "fix add"}, {"title": "run the tests"}]})
        ),
        tool_response(
            ("u1", "task_update", {"updates": [{"id": 1, "status": "completed"}]}),
            ("w", "write_file", {"path": "calc.py", "content": FIXED_CALC}),
        ),
        text_response("Fixed."),  # task 2 still open: the loop reminds once
        tool_response(("u2", "task_update", {"updates": [{"id": 2, "status": "completed"}]})),
        text_response("Fixed add(); tests pass."),
    ]


async def test_rows_record_how_the_plan_was_used(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(planned_solve_script()))
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0
    assert row["plan"] == {
        "tasks": 2,
        "completed": 2,
        "cancelled": 0,
        "open": 0,
        "updates": 3,
        "task_calls": 3,
        "reminders": 1,
        "plan_only_steps": 2,  # step 1 (create) and step 4 (complete task 2)
        "budget_credit": 2,  # both within the allowance
    }
    trace = json.loads((cfg.variant_dir / "traces" / "fix-add-bug_rep0.json").read_text())
    assert "task_create" in trace[0]["content"]  # the system prompt the model really got
    assert any(t["role"] == "user" and "unfinished tasks" in t["content"] for t in trace)
    summary = summarize(cfg.variant_dir)
    assert "plan made in 1/1 trials (median 2 tasks)" in summary
    assert "reminded in 1 (passed after reminder: 1/1)" in summary
    assert "task_* = 75% of tool calls · plan-only steps 2/5 (2 not counted" in summary


async def test_planning_off_rows_have_no_plan(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script()))
    cfg.settings = cfg.settings.model_copy(update={"planning": False})
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = rows(cfg)
    assert row["plan"] is None
    assert "planning:" not in summarize(cfg.variant_dir)


async def test_summary_counts_trials_that_ran_out_of_steps(tmp_path: Path) -> None:
    looping = [tool_response((f"l{i}", "list_dir", {})) for i in range(40)]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(looping)))
    cfg.settings = cfg.settings.model_copy(update={"max_steps": 3})
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    assert rows(cfg)[0]["run_status"] == "max_steps"
    assert "- ended at max_steps: 1 trial(s) (fix-add-bug)" in summarize(cfg.variant_dir)


def recall_script() -> list[LLMResponse | LLMError]:
    """Run 1 looks the answer up; run 2 writes it from memory without the log."""
    incident = json.dumps({"venue": "ARCX", "reason_code": "R07"})
    return [
        tool_response(("g", "bash", {"command": "grep 2024-03-15 logs/oms.log | wc -l"})),
        text_response("ARCX rejected the most orders on 2024-03-15; top reason R07."),
        tool_response(("w", "write_file", {"path": "incident.json", "content": incident})),
        text_response("Recorded."),
    ]


async def test_multi_run_task_runs_in_one_session_and_grades_the_trajectory(
    tmp_path: Path,
) -> None:
    providers: list[ScriptedProvider] = []

    def factory(s: Settings) -> ScriptedProvider:
        providers.append(ScriptedProvider(recall_script()))
        return providers[-1]

    cfg = cfg_for(tmp_path, factory)
    await run_suite(load_tasks(["recall-across-runs"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]
    assert [r["status"] for r in row["runs"]] == ["completed", "completed"]
    assert row["steps"] == 4 and len(row["meta"]["events"]) == 2
    # run 2 was sent run 1's conversation: that is what made the re-read unnecessary
    run2_first = providers[0].requests[2].messages
    assert run2_first[0]["content"].startswith("Our order management system log")
    assert run2_first[-1]["content"].startswith("Good. Record that")
    trace = json.loads((cfg.variant_dir / "traces" / "recall-across-runs_rep0.json").read_text())
    assert [t["content"][:4] for t in trace if t["role"] == "user"] == ["Our ", "Good"]


async def test_memory_off_runs_each_goal_fresh(tmp_path: Path) -> None:
    providers: list[ScriptedProvider] = []

    def factory(s: Settings) -> ScriptedProvider:
        providers.append(ScriptedProvider(recall_script()))
        return providers[-1]

    cfg = cfg_for(tmp_path, factory)
    cfg.settings = cfg.settings.model_copy(update={"memory": False})
    await run_suite(load_tasks(["recall-across-runs"]), cfg)
    [row] = rows(cfg)
    assert row["meta"]["memory"] is False
    assert providers[0].requests[2].messages == [
        {"role": "user", "content": load_tasks(["recall-across-runs"])[0].run_specs[1].goal}
    ]


async def test_between_hook_changes_the_world_between_runs(tmp_path: Path) -> None:
    # stale-fact: the rate file is refreshed after run 1; a run 2 that re-reads it passes
    script: list[LLMResponse | LLMError] = [
        tool_response(("r", "read_file", {"path": "config/fx.toml"})),
        text_response("1,356,250.00 USD at 1.0850."),
        tool_response(("r2", "read_file", {"path": "config/fx.toml"})),
        tool_response(("w", "write_file", {"path": "usd.json", "content": '{"usd": 1365000.0}'})),
        text_response("Done, at the refreshed 1.0920."),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    await run_suite(load_tasks(["stale-fact"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]


OMS_LOG_SHA = "5f59700c4a849992f2d01285c5e9d06100997a5eb28249acd4666aecb8581dd5"


def test_recall_log_is_frozen(tmp_path: Path) -> None:
    """Scores are comparable across runs only if every trial sees the same bytes."""
    import hashlib

    from evals.harness import TASKS_DIR, load_task_module

    load_task_module(TASKS_DIR / "recall-across-runs", "setup").setup(tmp_path)
    digest = hashlib.sha256((tmp_path / "logs" / "oms.log").read_bytes()).hexdigest()
    assert digest == OMS_LOG_SHA
