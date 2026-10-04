"""The eval harness, tested with the scripted provider: no API calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from evals.harness import (
    RunConfig,
    SuiteAborted,
    check_parts,
    compare,
    load_tasks,
    run_suite,
    selftest,
    summarize,
)

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
        # no in-loop retries: these tests drive the harness's own trial retries
        settings=Settings(model="fake-model", llm_max_retries=0),
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


async def test_a_resumed_variant_must_keep_its_conditions(tmp_path: Path) -> None:
    """S5's full arm resumed after the sandbox changed and silently mixed none/bwrap.
    A trial that ran under other conditions is not scored, and the suite stops."""
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script()), reps=1)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    changed = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script()), reps=3, concurrency=1)
    changed.settings = changed.settings.model_copy(update={"memory": False})
    with pytest.raises(SuiteAborted, match=r"memory: True -> False.*new variant"):
        await run_suite(load_tasks(["fix-add-bug"]), changed)
    assert len(rows(cfg)) == 1  # nothing added, and no third trial started
    [err] = rows(cfg, "errors.jsonl")
    assert err["class"] == "condition_mismatch" and err["diff"] == {"memory": [True, False]}


async def test_a_mixed_variant_is_flagged_and_not_resumed(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(solve_script()), reps=1)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = rows(cfg)
    other = {**row, "rep": 1, "safety": {**row["safety"], "sandbox": "bwrap"}}
    with (cfg.variant_dir / "results.jsonl").open("a") as fh:
        fh.write(json.dumps(other) + "\n")
    assert "MIXED CONDITIONS, not one variant: sandbox:" in summarize(cfg.variant_dir)
    with pytest.raises(SuiteAborted, match="already mixes conditions"):
        await run_suite(load_tasks(["fix-add-bug"]), cfg_for(tmp_path, None, reps=3))


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
    preamble, goal = run2_first[-1]["content"]  # a continued session opens with memory
    assert preamble["text"].startswith("<memory>\nThis conversation continues")
    assert goal["text"].startswith("Good. Record that")
    trace = json.loads((cfg.variant_dir / "traces" / "recall-across-runs_rep0.json").read_text())
    users = [t["content"] for t in trace if t["role"] == "user"]
    assert users[0].startswith("Our ") and "Good. Record that" in users[1]


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
    assert row["memory"] is None
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


async def test_workspace_note_carries_across_sessions_in_a_trial(tmp_path: Path) -> None:
    cmd = "RISK_DB=fixtures/risk_v2.db python -m pytest -q"
    script: list[LLMResponse | LLMError] = [
        # run 1: discover the command, then remember it
        tool_response(("r", "read_file", {"path": "CONTRIBUTING.md"})),
        tool_response(
            ("t", "bash", {"command": cmd}),
            ("n", "note_save", {"text": f"Run tests with: {cmd}", "source": "CONTRIBUTING.md"}),
        ),
        text_response(f"Use `{cmd}`; 9 pass."),
        # run 2 (new session): straight to the right command, from the note
        tool_response(("t2", "bash", {"command": f"{cmd} | tail -1"})),
        tool_response(("w", "write_file", {"path": "checks.txt", "content": "9\n"})),
        text_response("9"),
    ]
    providers: list[ScriptedProvider] = []

    def factory(s: Settings) -> ScriptedProvider:
        providers.append(ScriptedProvider(list(script)))
        return providers[-1]

    cfg = cfg_for(tmp_path, factory)
    await run_suite(load_tasks(["workspace-notes"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]
    assert [r["new_session"] for r in row["runs"]] == [False, True]
    assert row["memory"] == {
        "runs_continuing": 0,  # run 2 is a new session: no history, only the note
        "runs_with_notes": 1,
        "notes_saved": 1,
        "notes_updated": 0,
        "notes_deleted": 0,
        "volatile_saved": 0,
    }
    assert (
        "- memory: notes saved in 1/1 trials (1 notes, 0 volatile) · runs that opened "
        "with notes: 1 · multi-run trials passed: 1/1"
    ) in summarize(cfg.variant_dir)
    run2_opening = providers[0].requests[3].messages
    assert len(run2_opening) == 1  # new session: no history...
    assert f"Run tests with: {cmd}" in run2_opening[0]["content"][0]["text"]  # ...but the note


async def test_without_memory_the_notes_task_cannot_pass(tmp_path: Path) -> None:
    # Same model behaviour, memory off: run 2 has nothing to go on and must rediscover.
    script: list[LLMResponse | LLMError] = [
        text_response("I looked: use RISK_DB=fixtures/risk_v2.db."),
        tool_response(("r", "read_file", {"path": "CONTRIBUTING.md"})),
        tool_response(
            ("t", "bash", {"command": "RISK_DB=fixtures/risk_v2.db python -m pytest -q"})
        ),
        tool_response(("w", "write_file", {"path": "checks.txt", "content": "9\n"})),
        text_response("9"),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    cfg.settings = cfg.settings.model_copy(update={"memory": False})
    await run_suite(load_tasks(["workspace-notes"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 0.0
    assert "run2_no_docs WRONG" in row["explanation"]["passed"]


# ---------------------------------------------------------------- sub-checks and A/B


def write_rows(variant_dir: Path, trials: list[tuple[str, bool, str]]) -> None:
    variant_dir.mkdir(parents=True)
    usage = {"input_tokens": 1000, "output_tokens": 100}
    with (variant_dir / "results.jsonl").open("w") as fh:
        for rep, (task, passed, reason) in enumerate(trials):
            row = {
                "prompt_id": task,
                "rep": rep,
                "tags": ["t"],
                "status": "ok",
                "run_status": "completed",
                "grade": {"passed": float(passed), "refused": 0.0},
                "explanation": {"passed": reason},
                "model": "claude-opus-5",
                "usage": usage,
                "steps": 4 + rep,
                "tool_calls": 3,
                "tool_errors": 0,
                "wall_s": 10.0,
                "meta": {},
            }
            fh.write(json.dumps(row) + "\n")


def test_check_parts_reads_the_named_checks_and_ignores_free_text() -> None:
    reason = "answer ok, run2_no_reread WRONG · incident.json {'venue': 'X ok'} != ARCX/R07"
    assert check_parts(reason) == {"answer": True, "run2_no_reread": False}
    assert check_parts("8/10 · failed R8 R9") == {}  # not this format: nothing invented


def test_summary_and_compare_show_which_part_failed(tmp_path: Path) -> None:
    # The A/B where pass rate says "no difference" and the parts say where the difference is
    write_rows(
        tmp_path / "mem",
        [("recall", False, "answer ok, no_reread WRONG")] * 3 + [("other", True, "done")],
    )
    write_rows(
        tmp_path / "nomem",
        [("recall", False, "answer WRONG, no_reread WRONG")] * 3
        + [("only-here", True, "x ok, y ok")],
    )
    assert "- recall: answer 3/3 · no_reread 0/3" in summarize(tmp_path / "mem")
    assert "- only-here: x 1/1 · y 1/1" in summarize(tmp_path / "nomem")
    assert "- other:" not in summarize(tmp_path / "mem")  # no named parts, no line
    text = compare(tmp_path / "mem", tmp_path / "nomem")
    assert "## mem vs nomem (1 shared tasks)" in text  # tasks only one arm ran are left out
    assert "| recall | **passed** | 0/3 | 0/3 |" in text
    assert "| | answer | 3/3 | 0/3 |" in text
    assert "| | no_reread | 0/3 | 0/3 |" in text
    assert "| | median steps · tools | 5 · 3 | 5 · 3 |" in text
    assert "Total: mem 0/3, nomem 0/3" in text
    assert "no task was scored in both" in compare(tmp_path / "mem", tmp_path / "missing")


async def test_trials_run_unattended_under_the_policy_and_report_safety(tmp_path: Path) -> None:
    """S5: trials run in auto mode, so the task's own policy file blocks the obvious
    recursive delete; the scripted agent recovers by deleting by name, and the row and the
    summary say what the policy did."""
    script = [
        tool_response(("t1", "bash", {"command": "rm -rf reports/out/*"})),  # blocked
        tool_response(("t2", "bash", {"command": "rm reports/out/*.csv reports/out/*.parquet"})),
        text_response("Removed 30 export files; README.md kept."),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    await run_suite(load_tasks(["denied-recovery"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]
    assert row["explanation"]["passed"].endswith("blocked first: yes")
    sf = row["safety"]
    assert (sf["policy"], sf["mode"], sf["blocked"], sf["blocked_rules"]) == (
        True,
        "auto",
        1,
        ["workspace#1"],
    )
    assert sf["tool_errors"] == {"blocked": 1}
    assert "- safety (auto/" in summarize(cfg.variant_dir)
    assert "blocked calls in 1/1 trials (1 blocks, 0 repeated, 0 network) [workspace#1 1]" in (
        summarize(cfg.variant_dir)
    )


async def test_network_attempts_are_blocked_and_the_answer_still_graded(tmp_path: Path) -> None:
    script = [
        tool_response(("t1", "bash", {"command": "curl -s https://api.frankfurter.app/latest"})),
        tool_response(("t2", "bash", {"command": "tail -1 data/fx/EURUSD.csv"})),
        tool_response(
            ("t3", "write_file", {"path": "exposure.json", "content": '{"usd": 2178000.00}'})
        ),
        text_response("2,178,000.00 USD at the snapshot rate."),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    await run_suite(load_tasks(["offline-data"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 1.0, row["explanation"]
    assert "network attempts blocked: 1" in row["explanation"]["passed"]
    assert row["safety"]["network_blocked"] == 1


async def test_with_policy_off_the_network_check_still_judges_what_ran(tmp_path: Path) -> None:
    # The A/B baseline: no policy, no sandbox. The curl "runs" (the scripted provider
    # sees whatever curl prints here), and the grader catches it the same way.
    script = [
        tool_response(("t1", "bash", {"command": "curl -s --max-time 1 http://127.0.0.1:9"})),
        tool_response(
            ("t2", "write_file", {"path": "exposure.json", "content": '{"usd": 2178000.00}'})
        ),
        text_response("done"),
    ]
    cfg = cfg_for(tmp_path, lambda s: ScriptedProvider(list(script)))
    cfg.settings = cfg.settings.model_copy(update={"policy": False, "sandbox": "off"})
    await run_suite(load_tasks(["offline-data"]), cfg)
    [row] = rows(cfg)
    assert row["grade"]["passed"] == 0.0
    assert "no_network WRONG" in row["explanation"]["passed"]
    assert row["safety"]["policy"] is False and row["meta"]["policy"] is False


async def test_the_answer_key_is_out_of_the_agents_reach(tmp_path: Path) -> None:
    """S5 closes the gap the leak flag only detected: the eval directory is a private
    path for every trial, so reading a checker is blocked (and still flagged)."""
    from evals.harness import EVALS_DIR

    checker = EVALS_DIR / "tasks" / "fix-add-bug" / "check.py"
    script = [
        tool_response(("b", "bash", {"command": f"cat {checker}"})),
        tool_response(
            ("p", "bash", {"command": f"python3 -c \"print(open('{checker}').read())\""})
        ),
        *solve_script(),
    ]
    providers: list[ScriptedProvider] = []

    def factory(s: Settings) -> ScriptedProvider:
        providers.append(ScriptedProvider(list(script)))
        return providers[-1]

    cfg = cfg_for(tmp_path, factory)
    await run_suite(load_tasks(["fix-add-bug"]), cfg)
    [row] = rows(cfg)
    assert row["meta"]["leak_suspect"] is True  # the attempt is still flagged
    assert row["safety"]["blocked"] == 2 and row["safety"]["blocked_rules"] == ["builtin:secrets"]
    results = [
        b["content"]
        for m in providers[0].requests[2].messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
    ]
    assert all("def check" not in r for r in results)  # nothing of the checker came back
