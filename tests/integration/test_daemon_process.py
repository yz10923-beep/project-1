"""End-to-end: real kama-core process, real `kama` CLI process."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.conftest import Daemon, free_port, spawn_daemon


@pytest.fixture
def fake_stack() -> Iterator[Daemon]:
    """A real kama-core whose model is scripts/fake_api.py (fast mode) over HTTP."""
    api_port = free_port()
    api = subprocess.Popen(
        [sys.executable, "scripts/fake_api.py", str(api_port)],
        env={**os.environ, "FAKE_API_FAST": "1"},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert api.stdout is not None and "listening" in api.stdout.readline()
    d = spawn_daemon(
        free_port(),
        {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{api_port}", "ANTHROPIC_API_KEY": "fake"},
    )
    yield d
    d.proc.terminate()
    d.proc.wait(timeout=5)
    api.terminate()
    api.wait(timeout=5)


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
