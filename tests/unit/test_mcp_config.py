from pathlib import Path

import pytest

from kama_claude.core.agent.runner import load_policy
from kama_claude.core.config import Settings
from kama_claude.core.mcp.config import (
    McpConfigError,
    McpFile,
    McpServerConfig,
    dump_mcp_file,
    load_mcp_file,
)


def test_no_file_means_no_servers(tmp_path: Path) -> None:
    assert load_mcp_file(tmp_path / "missing.toml").servers == {}


def test_dump_reads_back_equal(tmp_path: Path) -> None:
    f = McpFile(
        servers={
            "ledger": McpServerConfig(
                command=["/usr/bin/python3", "led ger.py", 'say "hi"\\'],
                cwd=tmp_path,
                env={"MCP_CALL_LOG": str(tmp_path / "calls.jsonl"), "ÜNI": "ß\n"},
                trust=True,
                call_timeout_s=5.5,
            ),
            "web-search": McpServerConfig(
                transport="http", url="https://mcp.example.com/mcp", bearer_env="TOKEN"
            ),
        }
    )
    path = tmp_path / "mcp.toml"
    path.write_text(dump_mcp_file(f))
    assert load_mcp_file(path) == f


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("[servers.a]\ntransport = 'stdio'\n", "needs `command`"),
        ("[servers.a]\ntransport = 'http'\nurl = 'ftp://x'\n", "needs `url`"),
        ("[servers.a]\ncommand = ['x']\nurl = 'http://x'\n", "for http servers"),
        ("[servers.a__b]\ncommand = ['x']\n", "server name"),
        ("[servers.a]\ncommand = ['x']\nshell = true\n", "Extra inputs"),
        ("[servers.a\n", "a"),  # not TOML
    ],
)
def test_bad_files_are_errors_not_ignored(tmp_path: Path, text: str, complaint: str) -> None:
    path = tmp_path / "mcp.toml"
    path.write_text(text)
    with pytest.raises(McpConfigError, match=complaint):
        load_mcp_file(path)


def test_no_tool_call_may_read_or_write_the_mcp_file(tmp_path: Path) -> None:
    # Whoever writes it chooses what processes the next run starts.
    ws = tmp_path / "ws"
    ws.mkdir()
    mcp_file = tmp_path / "home" / "mcp.toml"
    settings = Settings(policy_file=tmp_path / "policy.toml", mcp_file=mcp_file)
    policy = load_policy(settings, ws, mode="auto")
    assert policy is not None
    for command in (f"cat {mcp_file}", f"echo '[servers.x]' >> {mcp_file}"):
        d = policy.check("bash", {"command": command})
        assert (d.action, d.rule) == ("deny", "builtin:secrets"), command
    # a neighbour is readable: the rule is about this file, not "outside the workspace"
    assert policy.check("bash", {"command": f"cat {mcp_file.parent}/x"}).action == "allow"
