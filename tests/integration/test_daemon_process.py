"""End-to-end: real kama-core process, real `kama` CLI process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from tests.conftest import Daemon, free_port, spawn_daemon


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
