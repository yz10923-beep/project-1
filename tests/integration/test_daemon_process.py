"""End-to-end: real kama-core process, real `kama` CLI process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import Daemon, free_port, spawn_daemon


@contextlib.contextmanager
def fake_stack_with(
    faults: str = "", daemon_env: dict[str, str] | None = None, **api_env: str
) -> Iterator[Daemon]:
    """A real kama-core whose model is scripts/fake_api.py (fast mode) over HTTP; `faults`
    makes its first requests fail (see FAKE_API_FAULTS in the script)."""
    api_port = free_port()
    api = subprocess.Popen(
        [sys.executable, "scripts/fake_api.py", str(api_port)],
        env={**os.environ, "FAKE_API_FAST": "1", "FAKE_API_FAULTS": faults, **api_env},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert api.stdout is not None and "listening" in api.stdout.readline()
    d = spawn_daemon(
        free_port(),
        {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api_port}",
            "ANTHROPIC_API_KEY": "fake",
            **(daemon_env or {}),
        },
    )
    try:
        yield d
    finally:
        d.proc.terminate()
        d.proc.wait(timeout=5)
        api.terminate()
        api.wait(timeout=5)


@pytest.fixture
def fake_stack() -> Iterator[Daemon]:
    with fake_stack_with() as d:
        yield d


def run_cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kama_claude.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_ping_against_daemon(daemon: Daemon) -> None:
    out = run_cli("ping", env=daemon.env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("pong server=")


def test_cli_ping_without_daemon_exits_3(tmp_path: Path) -> None:
    env = {**os.environ, "KAMA_PORT": str(free_port()), "KAMA_TOKEN_FILE": str(tmp_path / "t")}
    out = run_cli("ping", env=env)
    assert out.returncode == 3
    assert "is kama-core running?" in out.stderr


def test_token_file_is_private(daemon: Daemon) -> None:
    mode = Path(daemon.env["KAMA_TOKEN_FILE"]).stat().st_mode & 0o777
    assert mode == 0o600


def test_cli_lists_runs_and_rejects_unknown_ids(daemon: Daemon) -> None:
    out = run_cli("runs", env=daemon.env)
    assert out.returncode == 0 and "no runs" in out.stdout
    out = run_cli("cancel", "20990101-000000-000000", env=daemon.env)
    assert out.returncode == 1 and "unknown run" in out.stderr


def test_second_daemon_on_same_port_fails(daemon: Daemon) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "kama_claude.core"],
        env=daemon.env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert "cannot listen" in proc.stderr
    # The original daemon is unaffected.
    assert run_cli("ping", env=daemon.env).returncode == 0


def test_sigterm_shuts_down_cleanly() -> None:
    d = spawn_daemon(free_port())
    d.proc.send_signal(signal.SIGTERM)
    assert d.proc.wait(timeout=5) == 0


def test_planned_run_end_to_end_through_the_real_sdk(tmp_path: Path, fake_stack: Daemon) -> None:
    """S3 through every real layer: CLI -> daemon -> anthropic SDK -> HTTP/SSE (a fake
    Messages API) and back. The scripted model plans, stops early, is reminded, and
    finishes; the CLI shows the checklist, `runs` the progress, `trace` the cost."""
    d = fake_stack
    ws = tmp_path / "ws"
    ws.mkdir()
    out = run_cli("run", "-y", "-w", str(ws), "check python", env=d.env)
    assert out.returncode == 0, out.stderr
    for line in (
        "  plan (0/2)",
        "    [ ] 2. Report it  (blocked by 1)",
        "  [x] 1. Check the Python version  (1/2)",
        "  ! stopped with 1 open task(s); reminding the model of its plan",
        "plan: 2/2 completed · 0 cancelled · 0 open",
    ):
        assert line in out.stdout.splitlines(), (line, out.stdout)
    runs = run_cli("runs", env=d.env)
    assert "· plan 2/2" in runs.stdout
    trace = run_cli("trace", env={**d.env})
    assert trace.returncode == 0, trace.stderr
    assert "plan    2 tasks · 2 completed" in trace.stdout
    assert "3 of 6 steps only updated the plan" in trace.stdout  # 1 (create), 3, 5
    assert "time per task" in trace.stdout


async def test_tui_drives_a_planned_run_through_the_real_stack(
    tmp_path: Path, fake_stack: Daemon
) -> None:
    """The TUI (headless, via Textual's Pilot) against a real kama-core process that
    calls the fake Messages API through the real SDK."""
    from textual.widgets import Static

    from kama_claude.core.transport.client import JsonRpcClient, read_token
    from kama_claude.tui.app import KamaTui

    d = fake_stack
    token = read_token(Path(d.env["KAMA_TOKEN_FILE"]))
    ws = tmp_path / "ws"
    ws.mkdir()
    app = KamaTui(
        lambda: JsonRpcClient("127.0.0.1", d.port, token=token, client_name="kama-tui"),
        ws,
        goal="check python",
        auto_approve=True,
    )
    async with app.run_test(size=(150, 45)) as pilot:
        async with asyncio.timeout(30):
            while app.view is None or not app.view.finished:
                await pilot.pause(0.05)
        await pilot.pause()
        plan = str(app.query_one("#plan", Static).content)
        log = "\n".join(str(s.content) for s in app.query("#log Static"))
        status = str(app.query_one("#status", Static).content)
    assert "2/2 completed" in plan
    assert "reminded the model" in log and "■ completed after 6 steps" in log
    assert "Python 3" in log  # the bash output, inside the tool block
    assert "completed · step 6" in status and "plan 2/2" in status


def test_session_and_notes_end_to_end(tmp_path: Path, fake_stack: Daemon) -> None:
    """S4 through every real layer: run 1 starts a session and saves a note; run 2
    continues the session (history replayed from disk) and opens with that note."""
    d = fake_stack
    ws = tmp_path / "ws"
    ws.mkdir()
    first = run_cli("run", "-y", "--new-session", "-w", str(ws), "check python", env=d.env)
    assert first.returncode == 0, first.stderr
    assert "  ✎ note w1 added: python3 is on PATH here" in first.stdout
    sid = first.stdout.split("session ", 1)[1].split()[0]
    second = run_cli("run", "-y", "--session", sid, "-w", str(ws), "and now?", env=d.env)
    assert second.returncode == 0, second.stderr
    assert f"  session {sid}, continuing" in second.stdout
    assert "  memory: 1 note(s) sent before the goal" in second.stdout
    shown = run_cli("session", "show", sid, env=d.env)
    assert shown.stdout.count("completed") == 2
    notes = run_cli("notes", "list", "-w", str(ws), env=d.env)
    assert "[w1] python3 is on PATH here" in notes.stdout
    trace = run_cli("trace", env=d.env)
    assert f"memory  continues session {sid}" in trace.stdout


# ---------------------------------------------------------------- S5


def test_ping_reports_policy_and_sandbox(daemon: Daemon) -> None:
    out = run_cli("ping", env=daemon.env)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert "policy on" in lines
    assert any(line.startswith("sandbox ") for line in lines)


def test_model_call_failures_are_retried_through_the_real_sdk(tmp_path: Path) -> None:
    """Overloaded, overloaded mid-stream (after text was streamed), rate limited with
    retry-after: the SDK doesn't retry (max_retries=0), the loop does, visibly."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with fake_stack_with("529,stream,429@0.1") as d:
        out = run_cli("run", "-y", "-w", str(ws), "check python", env=d.env)
        trace = run_cli("trace", env=d.env)
    assert out.returncode == 0, out.stderr + out.stdout
    assert out.stdout.count("↻ model call failed") == 3
    assert "(the text above was cut off and is discarded)" in out.stdout
    assert "safety  mode auto · sandbox " in trace.stdout
    assert "3 model retries (" in trace.stdout and "retry wait" in trace.stdout
    assert "(overloaded)" in out.stdout and "(rate_limit)" in out.stdout
    run_dir = Path(out.stdout.rsplit("events: ", 1)[1].strip()).parent
    events = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
    retries = [e for e in events if e["type"] == "llm.retry"]
    assert [(e["attempt"], e["kind"]) for e in retries] == [
        (1, "overloaded"),
        (2, "overloaded"),
        (3, "rate_limit"),
    ]
    assert retries[2]["wait_s"] == 0.1  # the server's retry-after
    texts = json.dumps([e["content"] for e in events if e["type"] == "llm.response"])
    assert "cut off" not in texts  # the broken attempt left nothing in the conversation
    finished = events[-1]
    assert finished["type"] == "run.finished" and finished["llm_retries"] == 3


def test_permanent_api_errors_are_not_retried(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with fake_stack_with("400") as d:
        out = run_cli("run", "-y", "-w", str(ws), "check python", env=d.env)
    assert out.returncode == 1
    assert "model call failed" not in out.stdout, out.stdout
    assert "error: API error 400" in out.stdout


def test_a_prompt_past_the_window_ends_the_run_as_context_overflow(tmp_path: Path) -> None:
    """S6, over the real SDK: the API's 400 "prompt is too long" is the agent's failure
    (its history outgrew the window), not a retried or infra error."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with fake_stack_with(FAKE_API_MAX_PROMPT_CHARS="1500") as d:
        out = run_cli("run", "-y", "-w", str(ws), "check python", env=d.env)
    assert out.returncode == 1
    assert "== context_overflow after" in out.stdout, out.stdout
    assert "model call failed" not in out.stdout  # not retried
    run_dir = Path(out.stdout.rsplit("events: ", 1)[1].strip()).parent
    finished = json.loads((run_dir / "events.jsonl").read_text().splitlines()[-1])
    assert (finished["type"], finished["status"]) == ("run.finished", "context_overflow")


def test_a_session_past_its_budget_is_compacted_over_the_real_sdk(tmp_path: Path) -> None:
    """S6, end to end: the meter counts exactly near the budget, the daemon compacts at
    a step boundary, and the next request carries the block first with the beta header
    (the fake API answers 400 otherwise, as the real one does). The run still finishes
    its plan."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with fake_stack_with(
        daemon_env={"KAMA_CONTEXT_BUDGET": "10000"}, FAKE_API_TOKENS_PER_CHAR="5"
    ) as d:
        out = run_cli("run", "-y", "-w", str(ws), "check python", env=d.env)
    assert out.returncode == 0, out.stderr + out.stdout
    assert "⇣ context compacted:" in out.stdout and "plan: 2/2 completed" in out.stdout
    assert "context: budget 10,000 tokens" in out.stdout
    assert "of 10,000 budget · 1 compaction(s)" in out.stdout
    run_dir = Path(out.stdout.rsplit("events: ", 1)[1].strip()).parent
    events = [json.loads(x) for x in (run_dir / "events.jsonl").read_text().splitlines()]
    [compacted] = [e for e in events if e["type"] == "context.compacted"]
    assert compacted["measured"] == "count" and compacted["tokens_before"] > 10_000
    assert compacted["block"]["signature"].startswith("fake:")
    assert (events[-1]["status"], events[-1]["compactions"]) == ("completed", 1)
