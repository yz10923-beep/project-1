"""S5 in the agent loop: policy before approval, recovery messages, "always allow", typed
tool failures, model-call retries, the bash sandbox and its environment."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.agent.loop import AgentLoop, ApprovalDecision
from kama_claude.core.bus.events import (
    Event,
    LLMRetryEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolApprovalRequestedEvent,
    ToolApprovalResolvedEvent,
    ToolFinishedEvent,
    ToolPolicyEvent,
)
from kama_claude.core.llm.retry import RetryPolicy
from kama_claude.core.llm.types import LLMError, ToolCall
from kama_claude.core.policy.engine import Policy, Rule
from kama_claude.core.sandbox import Sandbox, bwrap_args, detect, scrubbed_env
from kama_claude.core.tools.base import ToolContext
from kama_claude.core.tools.builtin import Bash, BashParams, builtin_tools
from kama_claude.core.tools.registry import ToolRegistry
from tests.fakes import ScriptedProvider, text_response, tool_response


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def of[T](self, cls: type[T]) -> list[T]:
        return [e for e in self.events if isinstance(e, cls)]


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (d / "out").mkdir()
    (d / "out" / "a.csv").write_text("1\n")
    return d


def make(
    ws: Path,
    script: list[Any],
    *,
    mode: str = "auto",
    approver: Any = None,
    policy: bool = True,
    retry: RetryPolicy | None = None,
    sleeps: list[float] | None = None,
    remembered: list[tuple[Rule, ...]] | None = None,
) -> tuple[AgentLoop, ListSink, ScriptedProvider]:
    sink = ListSink()
    provider = ScriptedProvider(script)
    asked: list[ToolCall] = []

    async def default_approver(call: ToolCall) -> bool:
        asked.append(call)
        return True

    async def sleep(s: float) -> None:
        (sleeps if sleeps is not None else []).append(s)

    async def on_remember(rules: tuple[Rule, ...]) -> None:
        if remembered is not None:
            remembered.append(rules)

    loop = AgentLoop(
        provider=provider,
        registry=ToolRegistry(builtin_tools()),
        sink=sink,
        workspace=ws,
        approver=approver or default_approver,
        policy=Policy.load(ws, mode=mode, user_file=ws.parent / "none.toml")  # type: ignore[arg-type]
        if policy
        else None,
        retry=retry or RetryPolicy(max_retries=0),
        sleep=sleep,
        on_remember=on_remember,
    )
    return loop, sink, provider


def result_of(provider: ScriptedProvider, call_index: int, block: int = 0) -> dict[str, Any]:
    return provider.requests[call_index].messages[-1]["content"][block]  # type: ignore[no-any-return]


# ---------------------------------------------------------------- policy before approval


async def test_denied_rm_is_blocked_and_the_model_hears_why(ws: Path) -> None:
    loop, sink, p = make(
        ws,
        [
            tool_response(("t1", "bash", {"command": "rm -rf .git out"})),
            tool_response(("t2", "bash", {"command": "rm -rf out"})),
            text_response("removed out/, kept .git"),
        ],
    )
    r = await loop.run("clean up", "r1")
    assert r.status == "completed"
    assert (ws / ".git" / "HEAD").exists() and not (ws / "out").exists()  # blocked, then recovered
    blocked = result_of(p, 1)
    assert blocked["is_error"] and blocked["content"].startswith(
        "Blocked by policy (builtin:protected-path): rm deletes .git (recursive)"
    )
    [deny, allow] = sink.of(ToolPolicyEvent)
    assert (deny.action, deny.rule, deny.repeated) == ("deny", "builtin:protected-path", False)
    assert (allow.action, allow.kind) == ("allow", "delete")
    finished = sink.of(ToolFinishedEvent)
    assert [f.error_kind for f in finished] == ["blocked", None]
    [end] = sink.of(RunFinishedEvent)
    assert (end.policy_denials, end.repeat_denials, end.tool_errors) == (1, 0, {"blocked": 1})
    [started] = sink.of(RunStartedEvent)
    assert started.policy is not None and started.policy["mode"] == "auto"


async def test_trying_again_in_another_form_is_counted_and_answered_firmly(ws: Path) -> None:
    loop, sink, p = make(
        ws,
        [
            tool_response(("t1", "bash", {"command": "rm -rf .git"})),
            tool_response(("t2", "bash", {"command": "mv .git /tmp/hidden"})),  # same goal
            text_response("I can't remove .git"),
        ],
    )
    await loop.run("delete the repo history", "r1")
    assert result_of(p, 2)["content"].startswith("Blocked again by policy")
    assert [e.repeated for e in sink.of(ToolPolicyEvent)] == [False, True]
    [end] = sink.of(RunFinishedEvent)
    assert (end.policy_denials, end.repeat_denials) == (2, 1)


async def test_auto_mode_never_asks_and_never_opens_the_network(ws: Path) -> None:
    asked: list[ToolCall] = []

    async def approver(call: ToolCall) -> bool:
        asked.append(call)
        return True

    loop, sink, p = make(
        ws,
        [
            tool_response(("t1", "bash", {"command": "curl -s https://api.example.com/rate"})),
            text_response("used the local rate"),
        ],
        approver=approver,
    )
    await loop.run("rate?", "r1")
    assert asked == []  # -y can't override a deny, and nothing was asked
    assert "Network access is off for this run" in result_of(p, 1)["content"]


async def test_default_mode_reads_freely_and_asks_with_the_risk(ws: Path) -> None:
    asked: list[ToolCall] = []

    async def approver(call: ToolCall) -> ApprovalDecision:
        asked.append(call)
        return ApprovalDecision(False, "user", reason="use the archive script instead")

    loop, sink, p = make(
        ws,
        [
            tool_response(
                ("t1", "bash", {"command": "ls out"}), ("t2", "bash", {"command": "rm out/a.csv"})
            ),
            text_response("ok"),
        ],
        mode="default",
        approver=approver,
    )
    await loop.run("tidy", "r1")
    assert [c.id for c in asked] == ["t2"]  # `ls` needed nobody
    [req] = sink.of(ToolApprovalRequestedEvent)
    assert (req.risk, req.reason, req.remember) == (
        "medium",
        "rm deletes out/a.csv",
        ["bash: rm out/a.csv"],
    )
    denied = result_of(p, 1, block=1)
    assert "use the archive script instead" in denied["content"]
    assert (ws / "out" / "a.csv").exists()
    [resolved] = sink.of(ToolApprovalResolvedEvent)
    assert resolved.reason == "use the archive script instead"


async def test_always_allow_is_remembered_for_the_rest_of_the_session(ws: Path) -> None:
    asked: list[str] = []

    async def approver(call: ToolCall) -> ApprovalDecision:
        asked.append(call.input["command"])
        return ApprovalDecision(True, "user", remember=True)

    kept: list[tuple[Rule, ...]] = []
    loop, sink, _ = make(
        ws,
        [
            tool_response(("t1", "bash", {"command": "python -m pytest -q"})),
            tool_response(("t2", "bash", {"command": "python -m pytest -x"})),
            text_response("done"),
        ],
        mode="default",
        approver=approver,
        remembered=kept,
    )
    await loop.run("test", "r1")
    assert asked == ["python -m pytest -q"]  # the second run of pytest wasn't asked
    assert [r.command for r in kept[0]] == ["python -m pytest"]
    resolved = sink.of(ToolApprovalResolvedEvent)
    assert resolved[0].remembered == ["bash: python -m pytest"]


async def test_policy_off_is_the_s4_approval_flow(ws: Path) -> None:
    asked: list[str] = []

    async def approver(call: ToolCall) -> bool:
        asked.append(call.input["command"])
        return True

    loop, sink, _ = make(
        ws,
        [tool_response(("t1", "bash", {"command": "echo hi"})), text_response("ok")],
        approver=approver,
        policy=False,
    )
    await loop.run("x", "r1")
    assert asked == ["echo hi"] and sink.of(ToolPolicyEvent) == []
    [started] = sink.of(RunStartedEvent)
    assert started.policy is None


# ---------------------------------------------------------------- typed failures


async def test_tool_failures_have_kinds_and_repeats_are_called_out(ws: Path) -> None:
    same = ("read_file", {"path": "missing.txt"})
    loop, sink, p = make(
        ws,
        [
            tool_response(("t1", "read_file", {"pth": "x"})),  # invalid input
            tool_response(("t2", *same)),
            tool_response(("t3", *same)),
            tool_response(("t4", *same)),
            text_response("gave up"),
        ],
    )
    await loop.run("read", "r1")
    invalid = result_of(p, 1)["content"]
    assert invalid.splitlines()[0] == "Invalid input for read_file:"
    assert (
        "- path: Field required" in invalid and "- pth: Extra inputs are not permitted" in invalid
    )
    assert 'Expected: {"path": str, "offset"?: int, "limit"?: int}' in invalid
    assert "failed 3 times" not in result_of(p, 3)["content"]
    assert "This exact call has now failed 3 times" in result_of(p, 4)["content"]
    [end] = sink.of(RunFinishedEvent)
    assert end.tool_errors == {"invalid_input": 1, "not_found": 3}


# ---------------------------------------------------------------- model-call retries


async def test_retryable_errors_are_retried_with_backoff_and_reported(ws: Path) -> None:
    sleeps: list[float] = []
    loop, sink, _ = make(
        ws,
        [
            LLMError("API error 529: overloaded", retryable=True, kind="overloaded", status=529),
            LLMError("rate limited", retryable=True, kind="rate_limit", retry_after_s=2.5),
            text_response("done"),
        ],
        retry=RetryPolicy(max_retries=3, base_s=1, max_delay_s=30),
        sleeps=sleeps,
    )
    r = await loop.run("x", "r1")
    assert r.status == "completed"
    retries = sink.of(LLMRetryEvent)
    assert [(e.attempt, e.kind) for e in retries] == [(1, "overloaded"), (2, "rate_limit")]
    assert 0.5 <= sleeps[0] <= 1.0  # jittered backoff: between half and all of base * 2^0
    assert sleeps[1] == 2.5  # the server's retry-after wins
    [end] = sink.of(RunFinishedEvent)
    assert end.llm_retries == 2


async def test_permanent_errors_are_not_retried(ws: Path) -> None:
    sleeps: list[float] = []
    loop, sink, p = make(
        ws,
        [LLMError("API error 400: bad", retryable=False, kind="invalid_request")],
        retry=RetryPolicy(max_retries=3),
        sleeps=sleeps,
    )
    r = await loop.run("x", "r1")
    assert (r.status, r.retryable, sleeps, len(p.requests)) == ("error", False, [], 1)
    assert sink.of(LLMRetryEvent) == []


async def test_retries_stop_at_the_limit_and_at_the_budget(ws: Path) -> None:
    overloaded = LLMError("overloaded", retryable=True, kind="overloaded")
    loop, _, p = make(ws, [overloaded] * 3, retry=RetryPolicy(max_retries=2, base_s=0.01))
    r = await loop.run("x", "r1")
    assert r.status == "error" and r.error == "overloaded (after 2 retries)" and r.retryable
    assert len(p.requests) == 3
    slow = LLMError("slow down", retryable=True, kind="rate_limit", retry_after_s=50)
    loop, _, p = make(ws, [slow] * 5, retry=RetryPolicy(max_retries=5, budget_s=60))
    r = await loop.run("x", "r1")
    assert len(p.requests) == 2  # 50s waited; another 50s would pass the 60s budget


async def test_cancel_during_backoff_still_finishes_the_run(ws: Path) -> None:
    sink = ListSink()
    gate = asyncio.Event()

    async def slow_sleep(s: float) -> None:
        gate.set()
        await asyncio.sleep(3600)

    loop = AgentLoop(
        provider=ScriptedProvider([LLMError("x", retryable=True, kind="overloaded")]),
        registry=ToolRegistry(builtin_tools()),
        sink=sink,
        workspace=ws,
        approver=lambda c: asyncio.sleep(0, True),
        retry=RetryPolicy(max_retries=3),
        sleep=slow_sleep,
    )
    task = asyncio.create_task(loop.run("x", "r1"))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    [end] = sink.of(RunFinishedEvent)
    assert end.status == "cancelled"


# ---------------------------------------------------------------- the sandbox and env


async def run_bash(ws: Path, command: str, sandbox: Sandbox, network: bool) -> str:
    ctx = ToolContext(workspace=ws, sandbox=sandbox, network=network)
    return (await Bash().run(BashParams(command=command), ctx)).content


NET_PROBE = (
    f'{sys.executable} -c "import socket,sys; s=socket.socket(); s.settimeout(2); '
    "s.connect(('127.0.0.1', int(sys.argv[1]))); print('connected')\" {port}"
)


@pytest.mark.skipif(detect("auto").backend == "none", reason="no sandbox backend here")
async def test_without_network_permission_bash_cannot_reach_the_host(ws: Path) -> None:
    # A server on the host's loopback: reachable from the host's network namespace only.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    try:
        sandbox = detect("auto")
        blocked = await run_bash(ws, NET_PROBE.format(port=port), sandbox, network=False)
        allowed = await run_bash(ws, NET_PROBE.format(port=port), sandbox, network=True)
    finally:
        server.close()
    assert "connected" not in blocked and "exit_code: 1" in blocked
    assert "connected" in allowed


@pytest.mark.skipif(detect("auto").backend == "none", reason="no sandbox backend here")
async def test_local_servers_inside_the_sandbox_still_work(ws: Path) -> None:
    script = (
        f"{sys.executable} -c \"import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        "s.listen(); c=socket.create_connection(s.getsockname()); print('loopback ok')\""
    )
    assert "loopback ok" in await run_bash(ws, script, detect("auto"), network=False)


async def test_credentials_never_reach_bash(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("HARMLESS_SETTING", "visible")
    out = await run_bash(ws, "printenv", Sandbox("none"), network=True)
    assert "sk-ant-secret" not in out and "ghp_secret" not in out and "hunter2" not in out
    assert "HARMLESS_SETTING=visible" in out
    assert "GITHUB_TOKEN" in scrubbed_env(frozenset({"GITHUB_TOKEN"}))


def test_bwrap_keeps_history_read_only_and_hides_credentials(
    ws: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".kama").mkdir()
    monkeypatch.setenv("HOME", str(home))
    args = bwrap_args(ws, network=False)
    pairs = list(zip(args, args[1:], strict=False))
    assert ("--ro-bind", str(ws.resolve() / ".git")) in pairs
    assert ("--tmpfs", str(home / ".ssh")) in pairs and ("--tmpfs", str(home / ".kama")) in pairs
    assert "--unshare-net" in args and "--unshare-net" not in bwrap_args(ws, network=True)
    # the workspace is bound before .git is re-bound read-only over it
    assert args.index(str(ws.resolve())) < args.index(str(ws.resolve() / ".git"))


def test_explicit_backend_that_does_not_work_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from kama_claude.core import sandbox as sb

    detect.cache_clear()
    monkeypatch.setattr(sb.shutil, "which", lambda _: None)
    try:
        with pytest.raises(sb.SandboxUnavailable, match="bwrap is not installed"):
            detect("bwrap")
        assert detect("auto").backend == "none"
        assert "not installed" in detect("auto").note
    finally:
        detect.cache_clear()


async def test_always_allow_carries_over_to_the_next_run_of_the_session(ws: Path) -> None:
    from kama_claude.core.agent.runner import run_goal
    from kama_claude.core.config import Settings
    from kama_claude.core.session import SessionStore

    settings = Settings(
        runs_dir=ws.parent / "runs",
        sessions_dir=ws.parent / "sessions",
        memory_dir=ws.parent / "memory",
        policy_file=ws.parent / "policy.toml",
        sandbox="off",
    )
    store = SessionStore(settings.sessions_dir)
    sid = store.create(ws).session_id
    asked: list[str] = []

    async def always(call: ToolCall) -> ApprovalDecision:
        asked.append(call.input["command"])
        return ApprovalDecision(True, "user", remember=True)

    for goal in ("first", "second"):
        await run_goal(
            goal,
            settings=settings,
            workspace=ws,
            approver=always,
            provider=ScriptedProvider(
                [tool_response(("t", "bash", {"command": "make test"})), text_response("ok")]
            ),
            session_id=sid,
            sessions=store,
        )
    assert asked == ["make test"]  # asked in run 1 only
    assert [r.command for r in store.rules(sid)] == ["make test"]
