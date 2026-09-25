from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.tools.base import ToolContext, ToolError, resolve_in_workspace
from kama_claude.core.tools.builtin import builtin_tools
from kama_claude.core.tools.registry import MAX_RESULT_CHARS, ToolRegistry, truncate_middle


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("line1\nline2\nline3\n")
    return tmp_path


@pytest.fixture
def reg() -> ToolRegistry:
    return ToolRegistry(builtin_tools())


async def call(reg: ToolRegistry, ws: Path, name: str, **inp: Any) -> tuple[str, bool]:
    r = await reg.execute(name, inp, ToolContext(ws))
    return r.content, r.is_error


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "src/../../x"])
def test_paths_outside_workspace_are_rejected(ws: Path, bad: str) -> None:
    with pytest.raises(ToolError):
        resolve_in_workspace(ws, bad)


def test_symlink_escape_is_rejected(ws: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (ws / "link").symlink_to(outside)
    with pytest.raises(ToolError):
        resolve_in_workspace(ws, "link/secret.txt")


def test_specs_are_sorted_and_have_schemas(reg: ToolRegistry) -> None:
    specs = reg.specs()
    assert [s["name"] for s in specs] == ["bash", "list_dir", "read_file", "write_file"]
    read = next(s for s in specs if s["name"] == "read_file")
    assert read["input_schema"]["required"] == ["path"]
    assert read["input_schema"]["additionalProperties"] is False


async def test_read_file_numbers_lines_and_pages(reg: ToolRegistry, ws: Path) -> None:
    out, err = await call(reg, ws, "read_file", path="src/a.py", offset=2, limit=1)
    assert not err
    assert out.splitlines()[0] == "     2\tline2"
    assert "showing lines 2-2 of 3" in out


async def test_read_missing_file_is_model_visible_error(reg: ToolRegistry, ws: Path) -> None:
    out, err = await call(reg, ws, "read_file", path="nope.txt")
    assert err and "not a file" in out


async def test_read_binary_file_is_error(reg: ToolRegistry, ws: Path) -> None:
    (ws / "b.bin").write_bytes(b"\xff\xfe\x00")
    out, err = await call(reg, ws, "read_file", path="b.bin")
    assert err and "UTF-8" in out


async def test_list_dir_marks_directories_first(reg: ToolRegistry, ws: Path) -> None:
    (ws / "z.txt").write_text("")
    out, err = await call(reg, ws, "list_dir")
    assert not err
    assert out.splitlines() == ["src/", "z.txt"]


async def test_write_file_creates_parents(reg: ToolRegistry, ws: Path) -> None:
    out, err = await call(reg, ws, "write_file", path="new/dir/f.txt", content="hi")
    assert not err and out.startswith("Created")
    assert (ws / "new/dir/f.txt").read_text() == "hi"
    out, _ = await call(reg, ws, "write_file", path="new/dir/f.txt", content="again")
    assert out.startswith("Overwrote")


async def test_bash_reports_exit_code_and_runs_in_workspace(reg: ToolRegistry, ws: Path) -> None:
    out, err = await call(reg, ws, "bash", command="pwd; echo oops >&2; exit 3")
    assert not err  # a failing command is a result, not a tool failure
    assert out.splitlines()[0] == "exit_code: 3"
    assert os.path.realpath(ws) in out and "oops" in out


async def test_bash_timeout_kills_child_processes(reg: ToolRegistry, ws: Path) -> None:
    t0 = time.monotonic()
    out, err = await call(reg, ws, "bash", command="sleep 30 & sleep 30", timeout_s=1)
    assert err and "timed out" in out
    assert time.monotonic() - t0 < 5


async def test_unknown_tool_and_bad_input_are_errors(reg: ToolRegistry, ws: Path) -> None:
    out, err = await call(reg, ws, "rm_rf")
    assert err and "Unknown tool" in out
    out, err = await call(reg, ws, "read_file", path="a", bogus=1)
    assert err and "Invalid input" in out
    out, err = await call(reg, ws, "bash", command="true", timeout_s=9999)
    assert err and "Invalid input" in out


async def test_long_output_is_truncated_keeping_head_and_tail(reg: ToolRegistry, ws: Path) -> None:
    out, _ = await call(reg, ws, "bash", command="python3 -c \"print('A'*50000 + 'END')\"")
    assert len(out) < MAX_RESULT_CHARS + 200
    assert out.startswith("exit_code: 0") and out.rstrip().endswith("END")


def test_truncate_middle_is_noop_when_short() -> None:
    assert truncate_middle("abc", 10) == "abc"
