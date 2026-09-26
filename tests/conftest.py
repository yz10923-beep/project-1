from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class Daemon:
    proc: subprocess.Popen[str]
    port: int
    env: dict[str, str]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def spawn_daemon(port: int) -> Daemon:
    state = Path(tempfile.mkdtemp(prefix="kama-daemon-"))
    env = {
        **os.environ,
        "KAMA_PORT": str(port),
        "KAMA_LOG_LEVEL": "INFO",
        "KAMA_TOKEN_FILE": str(state / "core.token"),
        "KAMA_RUNS_DIR": str(state / "runs"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "kama_claude.core"],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait for the "listening" log line rather than sleeping a fixed amount.
    deadline = time.monotonic() + 10
    assert proc.stderr is not None
    while time.monotonic() < deadline:
        line = proc.stderr.readline()
        if "listening on" in line:
            return Daemon(proc, port, env)
        if not line and proc.poll() is not None:
            break
    proc.kill()
    raise RuntimeError(f"daemon failed to start (exit={proc.poll()})")


@pytest.fixture
def daemon() -> Iterator[Daemon]:
    d = spawn_daemon(free_port())
    yield d
    if d.proc.poll() is None:
        d.proc.terminate()
        d.proc.wait(timeout=5)
