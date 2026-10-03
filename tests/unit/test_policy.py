"""S5 permission policy: the bash parser, the classifier (through the labelled corpus), and
how rule sources and modes combine."""

from __future__ import annotations

from pathlib import Path

import pytest
from evals import policy_eval

from kama_claude.core.policy.engine import (
    Policy,
    PolicyFileError,
    Rule,
    denial_message,
)
from kama_claude.core.policy.shell import ShellSyntaxError, split_commands

# ---------------------------------------------------------------- parser


def argvs(src: str) -> list[list[str]]:
    return [[w.text for w in c.argv] for c in split_commands(src).commands]


def test_splits_on_every_control_operator_and_newline() -> None:
    assert argvs("a && b || c; d | e & f\ng") == [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"]]


def test_quotes_are_removed_but_not_split() -> None:
    assert argvs("r''m -rf '.git' \"a b\" c\\ d") == [["rm", "-rf", ".git", "a b", "c d"]]


def test_redirections_assignments_and_fds() -> None:
    [c] = split_commands("A=1 B='x y' pytest -q >>out.log 2>&1 </dev/null").commands
    assert [w.text for w in c.assigns] == ["A=1", "B=x y"]
    assert [(r.op, r.fd, r.target.text if r.target else None) for r in c.redirects] == [
        (">>", "", "out.log"),
        (">&", "2", None),
        ("<", "", "/dev/null"),
    ]


def test_heredoc_body_is_attached_not_parsed_as_commands() -> None:
    p = split_commands("python - <<'EOF'\nimport shutil\nrm -rf /\nEOF\necho after")
    assert argvs("python - <<'EOF'\nx\nEOF\necho after") == [["python", "-"], ["echo", "after"]]
    assert p.commands[0].redirects[0].heredoc == "import shutil\nrm -rf /"


def test_what_cannot_be_seen_is_marked_opaque() -> None:
    assert split_commands("$(echo rm) -rf x").opaque
    assert split_commands("echo `date`").opaque
    assert split_commands("diff <(sort a) b").opaque
    assert split_commands("eval 'x'").opaque
    assert split_commands("f() { rm x; }; f").opaque
    assert not split_commands("echo '$(not run)' $((1 + 2))").opaque  # quoted / arithmetic


def test_compound_commands_expose_their_inner_commands() -> None:
    assert argvs('for f in *.csv; do rm "$f"; done') == [["rm", "$f"]]
    assert argvs("if test -f x; then cat x; fi") == [["test", "-f", "x"], ["cat", "x"]]
    p = split_commands("ls | sh")
    assert [c.piped for c in p.commands] == [False, True]


@pytest.mark.parametrize("bad", ["echo 'open", 'echo "open', "echo $(open", "cat <"])
def test_unparsable_input_raises(bad: str) -> None:
    with pytest.raises(ShellSyntaxError):
        split_commands(bad)


# ---------------------------------------------------------------- the corpus (the gate)


def test_policy_corpus_never_allows_a_dangerous_command() -> None:
    report = policy_eval.run()
    assert report.dangerous_allowed == [], report.render()
    assert report.mismatches == [], report.render()
    assert len(report.results) >= 140
    # the known gaps are real: the classifier lets them through and says so
    assert report.gaps and all(r.dangerous_allow for r in report.gaps)


# ---------------------------------------------------------------- rule sources and modes


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    d = tmp_path / "ws"
    (d / ".git").mkdir(parents=True)
    (d / "raw").mkdir()
    (d / "raw" / "trades.csv").write_text("x")
    (d / "out").mkdir()
    return d


def policy(ws: Path, mode: str = "default", user: str = "", local: str = "") -> Policy:
    user_file = ws.parent / "user-policy.toml"
    user_file.write_text(user)
    if local:
        (ws / ".kama").mkdir(exist_ok=True)
        (ws / ".kama" / "policy.toml").write_text(local)
    return Policy.load(ws, mode=mode, user_file=user_file)  # type: ignore[arg-type]


def act(p: Policy, command: str) -> str:
    return p.check("bash", {"command": command}).action


def test_auto_mode_allows_work_but_never_what_needs_a_human(ws: Path) -> None:
    p = policy(ws, "auto")
    assert act(p, "rm -rf out") == "allow"
    assert act(p, "curl https://x.io") == "deny"  # nobody to ask: deny, not allow
    assert act(p, "git reset --hard") == "deny"
    assert act(p, "rm -rf .git") == "deny"


def test_read_only_mode_allows_only_reads(ws: Path) -> None:
    p = policy(ws, "read-only")
    assert act(p, "cat raw/trades.csv | wc -l") == "allow"
    assert act(p, "python -m pytest") == "deny"
    assert p.check("write_file", {"path": "a.txt"}).action == "deny"


def test_workspace_policy_can_only_tighten(ws: Path) -> None:
    local = """
[[rules]]
action = "deny"
path = "raw/**"
effect = ["delete", "write"]
reason = "raw/ is under retention"

[[rules]]
action = "allow"
command = "curl"
"""
    p = policy(ws, "auto", local=local)
    d = p.check("bash", {"command": "rm raw/trades.csv"})
    assert (d.action, d.rule, d.reason) == ("deny", "workspace#1", "raw/ is under retention")
    assert act(p, "rm -rf out") == "allow"  # only raw/ is covered
    assert act(p, "curl https://x.io") == "deny"  # its allow was ignored...
    assert any("ignored 1 allow rule" in w for w in p.warnings)  # ...and that is reported


def test_user_policy_can_allow_and_deny(ws: Path) -> None:
    user = """
mode = "auto"

[[rules]]
action = "allow"
command = "pip install"
reason = "this machine may install packages"

[[rules]]
action = "deny"
command = "git commit*"
"""
    p = Policy.load(ws, mode=None, user_file=_write(ws.parent / "u.toml", user))
    assert p.mode == "auto"  # the user file sets the default mode
    d = p.check("bash", {"command": "pip install requests"})
    assert (d.action, d.rule, d.network) == ("allow", "user#1", True)  # the sandbox opens
    assert act(p, "git commit -m x") == "deny"
    assert act(p, "pip install x && curl y") == "deny"  # the allow covers pip only


def test_workspace_deny_beats_user_allow(ws: Path) -> None:
    p = policy(
        ws,
        "auto",
        user='[[rules]]\naction = "allow"\ncommand = "rm"\n',
        local='[[rules]]\naction = "deny"\npath = "raw/**"\n',
    )
    assert act(p, "rm raw/trades.csv") == "deny"


def test_workspace_ask_becomes_deny_when_nobody_can_answer(ws: Path) -> None:
    local = '[[rules]]\naction = "ask"\ncommand = "python *"\nreason = "review scripts"\n'
    assert act(policy(ws, "default", local=local), "python x.py") == "ask"
    d = policy(ws, "auto", local=local).check("bash", {"command": "python x.py"})
    assert d.action == "deny" and "needs a human" in d.reason


def test_always_allow_lifts_asks_but_never_denies(ws: Path) -> None:
    p = policy(ws)
    d = p.check("bash", {"command": "python -m pytest -q && rm -rf .git"})
    assert d.action == "deny" and d.remember == ()  # nothing to remember for a deny
    d = p.check("bash", {"command": "python -m pytest -q"})
    assert d.action == "ask"
    assert [r.command for r in d.remember] == ["python -m pytest"]
    p.remember(d.remember)
    assert act(p, "python -m pytest -x") == "allow"
    assert act(p, "rm -rf .git") == "deny"


def test_bad_policy_file_is_an_error_not_silently_ignored(ws: Path) -> None:
    with pytest.raises(PolicyFileError):
        policy(ws, user='[[rules]]\naction = "maybe"\n')
    with pytest.raises(PolicyFileError):
        policy(ws, user="not toml [")


def test_rule_command_patterns() -> None:
    r = Rule(action="allow", command="git push")
    assert r.matches_command("git push origin main") and r.matches_command("git push")
    assert not r.matches_command("git pushy") and not r.matches_command("xgit push")
    g = Rule(action="deny", command="pytest *-k*")
    assert g.matches_command("pytest -q -k slow") and not g.matches_command("pytest -q")


def test_denial_messages_tell_the_model_what_to_do(ws: Path) -> None:
    p = policy(ws, "auto")
    net = denial_message(p.check("bash", {"command": "curl https://x.io"}))
    assert net.startswith("Blocked by policy (mode:auto): curl connects to other machines.")
    assert "Use the data in the workspace" in net
    git = denial_message(p.check("bash", {"command": "rm -rf .git"}))
    assert "Rewording the command or doing it another way" in git
    again = denial_message(p.check("bash", {"command": "rm -rf .git"}), repeated=True)
    assert "You already tried this" in again


def test_paths_through_symlinks_are_judged_by_their_target(ws: Path) -> None:
    (ws / "link").symlink_to(ws / ".git")
    assert act(policy(ws, "auto"), "rm -rf link/") == "deny"


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path
