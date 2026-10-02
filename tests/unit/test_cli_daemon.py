"""The CLI's daemon path (kama run / attach) against an in-process kama-core."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from kama_claude.cli import main as cli
from kama_claude.core.app import CoreApp, write_token
from kama_claude.core.bus.events import ToolApprovalRequestedEvent
from kama_claude.core.config import Settings
from tests.fakes import PausingProvider, ScriptedProvider, text_response, tool_response


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


async def test_kama_trace_reports_the_latest_run(
    core: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _, settings, ws = core
    assert await cli._run(settings, run_args(ws, yes=True)) == cli.EXIT_OK
    await asyncio.sleep(0.05)
    capsys.readouterr()
    args = argparse.Namespace(run_id=None, chrome=None, width=30)
    assert cli._trace(settings, args) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "where the time went" in out and "tool write_file" in out
    assert "event bus" in out and "ipc requests" in out
    chrome = tmp_path / "t.json"
    args = argparse.Namespace(run_id=None, chrome=str(chrome), width=30)
    assert cli._trace(settings, args) == cli.EXIT_OK
    assert json.loads(chrome.read_text())["traceEvents"]


def test_kama_trace_unknown_run_is_a_usage_error(core: Any) -> None:
    _, settings, _ = core
    args = argparse.Namespace(run_id="nope", chrome=None, width=30)
    assert cli._trace(settings, args) == cli.EXIT_USAGE


async def test_kama_plan_shows_and_steers_a_live_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = PausingProvider(
        [
            tool_response(
                ("c", "task_create", {"tasks": [{"title": "parse"}, {"title": "chart"}]})
            ),
            text_response("done"),
            text_response("done, really"),
        ],
        pause_at={1},
    )
    app = CoreApp(
        Settings(port=0, runs_dir=tmp_path / "runs", token_file=tmp_path / "tok"),
        provider_factory=lambda s: provider,
    )
    _, port = await app.server.start()
    write_token(tmp_path / "tok", app.token)
    settings = app.settings.model_copy(update={"port": port})
    try:
        run_id = (await app.runs.start("g", ws, auto_approve=True)).run_id
        await provider.paused.wait()
        parse = cli.build_parser().parse_args
        assert (
            await cli._plan(settings, parse(["plan", "add", run_id, "docs", "--after", "1"])) == 0
        )
        assert (
            await cli._plan(
                settings, parse(["plan", "cancel", run_id, "2", "--reason", "no charts"])
            )
            == 0
        )
        assert await cli._plan(settings, parse(["plan", "show", run_id])) == 0
        out = capsys.readouterr().out
        assert "added 3. docs (after 1)" in out
        assert "task 2: pending -> cancelled (no charts)" in out
        assert out.rstrip().endswith(
            "Plan (0/3 completed):\n"
            "[ ] 1. parse\n"
            "[-] 2. chart  (no charts)\n"
            "[ ] 3. docs  (blocked by 1; added by the user)"
        )
        provider.resume.set()
        handle = app.runs.runs[run_id]
        assert handle.task is not None
        await handle.task
        await cli._plan(settings, parse(["plan", "show", run_id]))
        assert capsys.readouterr().out.endswith("(run finished)\n")
    finally:
        await app.runs.shutdown()
        await app.server.stop()
