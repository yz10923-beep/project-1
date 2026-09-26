"""The CLI's daemon path (kama run / attach) against an in-process kama-core."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kama_claude.cli import main as cli
from kama_claude.core.app import CoreApp, write_token
from kama_claude.core.bus.events import ToolApprovalRequestedEvent
from kama_claude.core.config import Settings
from tests.fakes import ScriptedProvider, text_response, tool_response


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[tuple[CoreApp, Settings, Path]]:
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        tool_response(("w1", "write_file", {"path": "hello.txt", "content": "hi"})),
        text_response("Wrote hello.txt for you."),
    ]
    app = CoreApp(
        Settings(port=0, runs_dir=tmp_path / "runs", token_file=tmp_path / "tok"),
        provider_factory=lambda s: ScriptedProvider(list(script)),
    )
    _, port = await app.server.start()
    write_token(tmp_path / "tok", app.token)
    settings = app.settings.model_copy(update={"port": port})
    yield app, settings, ws
    await app.runs.shutdown()
    await app.server.stop()


def run_args(ws: Path, **kw: Any) -> argparse.Namespace:
    base = dict(
        goal="say hi",
        workspace=ws,
        model=None,
        max_steps=None,
        yes=False,
        detach=False,
        local=False,
    )
    return argparse.Namespace(**{**base, **kw})


async def test_kama_run_streams_and_completes(
    core: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _, settings, ws = core
    code = await cli._run(settings, run_args(ws, yes=True))
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "Wrote hello.txt for you." in out  # streamed text
    assert "== completed" in out
    assert (ws / "hello.txt").read_text() == "hi"


async def test_kama_run_without_tty_denies_side_effects(
    core: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _, settings, ws = core
    code = await cli._run(settings, run_args(ws))  # stdin is not a TTY under pytest
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "denied (non-interactive" in out
    assert not (ws / "hello.txt").exists()


async def test_attach_answers_approvals_from_a_second_terminal(
    core: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, settings, ws = core
    started = await cli._run(settings, run_args(ws, detach=True))
    assert started == cli.EXIT_OK
    run_id = capsys.readouterr().out.strip()

    async def approve(event: ToolApprovalRequestedEvent) -> bool | None:
        return True

    async with cli.connect(settings) as client:
        finished = await asyncio.wait_for(cli.watch(client, run_id, 0, approve), 10)
    assert finished is not None and finished.status == "completed"
    assert (ws / "hello.txt").read_text() == "hi"
