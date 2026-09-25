"""Real API calls. Costs money; run explicitly with `make live`."""

from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.agent.runner import run_goal
from kama_claude.core.config import load_settings
from kama_claude.core.llm.types import ToolCall

# Same resolution as `kama` itself: env vars, ./.env (repo root under `make live`), ~/.kama/.env.
SETTINGS = load_settings()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        SETTINGS.anthropic_api_key is None,
        reason="no ANTHROPIC_API_KEY in env, ./.env or ~/.kama/.env",
    ),
]


async def allow(_: ToolCall) -> bool:
    return True


async def test_agent_fixes_a_bug_and_verifies(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    result, run_dir = await run_goal(
        "The test in test_calc.py fails. Fix calc.py and confirm with "
        "`python -m pytest -q` (or plain python if pytest is missing).",
        settings=SETTINGS,
        workspace=tmp_path,
        approver=allow,
    )
    assert result.status == "completed", (run_dir / "events.jsonl").read_text()[-2000:]
    assert "a + b" in (tmp_path / "calc.py").read_text()
    assert result.steps <= 8
