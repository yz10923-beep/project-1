"""The permission policy (S5): every tool call gets allow, ask or deny, with the rule that
decided it and a reason the model and the user can read.

Inputs, from most to least authoritative:
- the user's policy file (~/.kama/policy.toml): may allow, ask or deny anything; its allow
  can lift a built-in verdict (it's the user's machine);
- the workspace's .kama/policy.toml: may only make things stricter (ask, deny), because the
  workspace is where the agent writes, so an allow there could be written by the agent;
- rules the user added during the session by answering "always" (they lift asks, never
  denies);
- built-in classification (core/policy/classify.py) mapped through the mode.

    mode            read   write  delete exec   network risky  forbidden
    default         allow  ask    ask    ask    ask     ask    deny
    accept-edits    allow  allow  ask    ask    ask     ask    deny
    auto (-y)       allow  allow  allow  allow  deny    deny   deny
    read-only       allow  deny   deny   deny   deny    deny   deny

`auto` is unattended: nobody can answer an ask, so the asks a human should see (network,
unverifiable, destructive) become denies instead of silent allows. `-y` never overrides a
deny.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kama_claude.core.policy.classify import SEVERITY, Analysis, Effect, EffectKind, analyze_bash
from kama_claude.core.policy.paths import PathContext, classify

Action = Literal["allow", "ask", "deny"]
Mode = Literal["default", "accept-edits", "auto", "read-only"]
MODES: tuple[Mode, ...] = ("default", "accept-edits", "auto", "read-only")
Risk = Literal["low", "medium", "high"]
_ORDER: dict[Action, int] = {"allow": 0, "ask": 1, "deny": 2}

MODE_TABLE: dict[Mode, dict[EffectKind, Action]] = {
    "default": {
        "read": "allow",
        "write": "ask",
        "delete": "ask",
        "exec": "ask",
        "network": "ask",
        "risky": "ask",
        "forbidden": "deny",
    },
    "accept-edits": {
        "read": "allow",
        "write": "allow",
        "delete": "ask",
        "exec": "ask",
        "network": "ask",
        "risky": "ask",
        "forbidden": "deny",
    },
    "auto": {
        "read": "allow",
        "write": "allow",
        "delete": "allow",
        "exec": "allow",
        "network": "deny",
        "risky": "deny",
        "forbidden": "deny",
    },
    "read-only": {
        "read": "allow",
        "write": "deny",
        "delete": "deny",
        "exec": "deny",
        "network": "deny",
        "risky": "deny",
        "forbidden": "deny",
    },
}
RISK: dict[EffectKind, Risk] = {
    "read": "low",
    "write": "medium",
    "delete": "medium",
    "exec": "medium",
    "network": "high",
    "risky": "high",
    "forbidden": "high",
}

# Tools that only read (or only touch the run's own plan and notes).
READ_TOOLS = {
    "read_file",
    "list_dir",
    "task_create",
    "task_update",
    "task_get",
    "task_list",
    "note_save",
    "note_update",
    "note_delete",
    "note_list",
}


class PolicyFileError(ValueError):
    pass


class Rule(BaseModel):
    """One rule from a policy file, or remembered from an "always" answer."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    tool: str = "*"  # bash, write_file, ... or *
    command: str | None = Field(
        default=None, description="bash: a word prefix ('git push') or a glob ('pytest *')."
    )
    effect: list[EffectKind] | None = None
    path: str | None = Field(default=None, description="glob, relative to the workspace")
    recursive: bool | None = None
    reason: str = ""
    id: str = ""

    def matches_command(self, command: str) -> bool:
        if self.command is None:
            return True
        pat = self.command.strip()
        if any(ch in pat for ch in "*?["):
            return fnmatch.fnmatchcase(command, pat)
        return command == pat or command.startswith(pat + " ")

    def matches(self, tool: str, effect: Effect, ws: Path) -> bool:
        if self.tool not in ("*", tool):
            return False
        if tool == "bash" and not self.matches_command(effect.command):
            return False
        if tool != "bash" and self.command is not None:
            return False
        if self.effect is not None and effect.kind not in self.effect:
            return False
        if self.recursive is not None and effect.recursive != self.recursive:
            return False
        if self.path is not None:
            rels = [_rel(p.path, ws) for p in effect.paths if p.path is not None]
            if not any(r is not None and _path_match(r, self.path) for r in rels):
                return False
        return True


def _rel(p: Path, ws: Path) -> str | None:
    try:
        return p.relative_to(ws).as_posix()
    except ValueError:
        return str(p)


def _path_match(rel: str, pattern: str) -> bool:
    pattern = pattern.rstrip("/")
    if pattern.endswith("/**"):
        base = pattern[:-3]
        return rel == base or rel.startswith(base + "/") or fnmatch.fnmatchcase(rel, pattern)
    return fnmatch.fnmatchcase(rel, pattern) or rel.startswith(pattern + "/")


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Mode | None = None
    rules: list[Rule] = Field(default_factory=list)


def load_policy_file(path: Path, *, label: str) -> PolicyFile:
    if not path.is_file():
        return PolicyFile()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        pf = PolicyFile.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, UnicodeDecodeError) as e:
        raise PolicyFileError(f"{path}: {e}") from e
    for i, r in enumerate(pf.rules):
        r.id = r.id or f"{label}#{i + 1}"
    return pf


@dataclass(frozen=True)
class Decision:
    action: Action
    rule: str  # what decided it: "builtin:protected-path", "user#2", "mode:auto", ...
    reason: str
    risk: Risk
    kind: EffectKind  # the effect that decided it
    effects: tuple[Effect, ...] = ()
    network: bool = False  # the call may use the network (sandbox opens it)
    remember: tuple[Rule, ...] = ()  # what "always allow" would add
    opaque: tuple[str, ...] = ()

    def to_event(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
            "risk": self.risk,
            "kind": self.kind,
            "network": self.network,
            "effects": [f"{e.kind}: {e.what}" for e in self.effects][:12],
        }


@dataclass
class Policy:
    """A run's policy: mode, rule sources, and rules added from "always" answers."""

    mode: Mode
    paths: PathContext
    user_rules: list[Rule] = field(default_factory=list)
    workspace_rules: list[Rule] = field(default_factory=list)
    session_rules: list[Rule] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(
        cls,
        workspace: Path,
        *,
        mode: Mode | None,
        user_file: Path,
        kama_paths: tuple[Path, ...] = (),
        session_rules: list[Rule] | None = None,
    ) -> Policy:
        user = load_policy_file(user_file.expanduser(), label="user")
        ws_file = workspace / ".kama" / "policy.toml"
        local = load_policy_file(ws_file, label="workspace")
        warnings = []
        kept = [r for r in local.rules if r.action != "allow"]
        if len(kept) < len(local.rules):
            warnings.append(
                f"{ws_file}: ignored {len(local.rules) - len(kept)} allow rule(s); a workspace "
                "policy can only make things stricter (put allows in ~/.kama/policy.toml)"
            )
        if local.mode is not None:
            warnings.append(
                f"{ws_file}: `mode` is ignored here; set it per run or in the user file"
            )
        return cls(
            mode=mode or user.mode or "default",
            paths=PathContext.for_workspace(workspace, (user_file.expanduser(), *kama_paths)),
            user_rules=user.rules,
            workspace_rules=kept,
            session_rules=list(session_rules or []),
            warnings=warnings,
        )

    # ---- deciding

    def effects_for(
        self, tool: str, tool_input: dict[str, Any]
    ) -> tuple[list[Effect], Analysis | None]:
        ws = self.paths.workspace
        if tool == "bash":
            analysis = analyze_bash(str(tool_input.get("command", "")), self.paths)
            return analysis.effects, analysis
        if tool in {"write_file", "read_file", "list_dir"}:
            raw = str(tool_input.get("path", "."))
            info = classify(raw, ws, self.paths)
            if tool == "write_file":
                kind: EffectKind = {
                    "protected": "forbidden",
                    "sensitive": "forbidden",
                    "outside": "forbidden",
                    "unknown": "risky",
                }.get(info.kind, "write")  # type: ignore[assignment]
                what = f"write_file writes {raw}" + {
                    "protected": ", which is protected (.git, .kama)",
                    "sensitive": ", which holds secrets",
                    "outside": ", outside the workspace",
                }.get(info.kind, "")
                rule = {
                    "protected": "protected-path",
                    "sensitive": "secrets",
                    "outside": "outside-workspace",
                }.get(info.kind, "")
                return [Effect(kind, what, raw, rule=rule, paths=(info,))], None
            if info.kind == "sensitive":
                return [
                    Effect(
                        "forbidden",
                        f"{tool} reads {raw}, which holds secrets",
                        raw,
                        rule="secrets",
                        paths=(info,),
                    )
                ], None
            return [Effect("read", f"{tool} only reads", raw, paths=(info,))], None
        if tool in READ_TOOLS:
            return [Effect("read", f"{tool} changes only the run's own plan or notes", tool)], None
        return [Effect("exec", f"{tool} is a tool the policy has no rules for", tool)], None

    def check(self, tool: str, tool_input: dict[str, Any]) -> Decision:
        effects, analysis = self.effects_for(tool, tool_input)
        ws = self.paths.workspace
        verdicts: list[tuple[Action, str, str, Effect]] = []
        for e in effects:
            verdicts.append(self._decide(tool, e, ws))
        action, rule, reason, effect = max(
            verdicts, key=lambda v: (_ORDER[v[0]], SEVERITY[v[3].kind])
        )
        network = any(e.kind == "network" for e in effects) and action != "deny"
        remember = self._remember(tool, effects, analysis) if action == "ask" else ()
        return Decision(
            action=action,
            rule=rule,
            reason=reason,
            risk=RISK[effect.kind],
            kind=effect.kind,
            effects=tuple(effects),
            network=network,
            remember=remember,
            opaque=tuple(analysis.opaque) if analysis else (),
        )

    def _decide(self, tool: str, e: Effect, ws: Path) -> tuple[Action, str, str, Effect]:
        for r in [*self.workspace_rules, *self.user_rules]:
            if r.action == "deny" and r.matches(tool, e, ws):
                return "deny", r.id, r.reason or f"denied by {r.id}: {e.what}", e
        builtin = MODE_TABLE[self.mode][e.kind]
        rule = f"builtin:{e.rule}" if e.rule else f"mode:{self.mode}"
        verdict: tuple[Action, str, str, Effect] = (builtin, rule, e.what, e)
        user_allow = next(
            (r for r in self.user_rules if r.action == "allow" and r.matches(tool, e, ws)), None
        )
        if user_allow is not None and not (self.mode == "read-only" and e.kind != "read"):
            verdict = ("allow", user_allow.id, user_allow.reason or e.what, e)
        elif builtin == "ask":
            session = next((r for r in self.session_rules if r.matches(tool, e, ws)), None)
            if session is not None:
                verdict = ("allow", session.id or "session", e.what, e)
        asks = [
            r
            for r in [*self.workspace_rules, *self.user_rules]
            if r.action == "ask" and r.matches(tool, e, ws)
        ]
        if asks and verdict[0] == "allow" and self.mode != "auto":
            verdict = ("ask", asks[0].id, asks[0].reason or e.what, e)
        elif asks and verdict[0] == "allow":
            verdict = ("deny", asks[0].id, (asks[0].reason or e.what) + " (needs a human)", e)
        return verdict

    def _remember(
        self, tool: str, effects: list[Effect], analysis: Analysis | None
    ) -> tuple[Rule, ...]:
        """The session rules an "always allow" answer adds: per command prefix for bash
        (program + subcommand), the whole tool otherwise. Never for forbidden effects."""
        if any(e.kind == "forbidden" for e in effects):
            return ()
        if tool != "bash":
            return (
                Rule(
                    action="allow",
                    tool=tool,
                    effect=["write"],
                    id=f"session:{tool}",
                    reason=f"you chose always allow for {tool}",
                ),
            )
        prefixes: list[str] = []
        for e in effects:
            if e.kind in {"read", "forbidden"}:
                continue
            words = e.command.split()
            if not words:
                continue
            if e.kind == "risky":
                prefix = e.command  # a risky command is remembered exactly, never by prefix
            else:
                # the program and its first argument that isn't an option: `python -m
                # pytest`, `npm test`, `git commit`; at most three words
                take = next(
                    (i + 1 for i, w in enumerate(words[1:4], 1) if not w.startswith("-")), 1
                )
                prefix = " ".join(words[:take])
            if prefix not in prefixes:
                prefixes.append(prefix)
        return tuple(
            Rule(
                action="allow",
                tool="bash",
                command=p,
                id=f"session:{p}",
                reason=f"you chose always allow for `{p}`",
            )
            for p in prefixes
        )

    def remember(self, rules: tuple[Rule, ...]) -> None:
        for r in rules:
            if all(r.model_dump() != s.model_dump() for s in self.session_rules):
                self.session_rules.append(r)

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "user_rules": [r.model_dump(exclude_none=True) for r in self.user_rules],
            "workspace_rules": [r.model_dump(exclude_none=True) for r in self.workspace_rules],
            "session_rules": [r.model_dump(exclude_none=True) for r in self.session_rules],
            "warnings": self.warnings,
        }


def denial_message(d: Decision, *, repeated: bool = False) -> str:
    """What the model reads when policy blocks a call: what, why, and what to do instead."""
    if repeated:
        return (
            f"Blocked again by policy ({d.rule}): {d.reason}. You already tried this and it was "
            "blocked. Do not try the same thing again in another form; it will be blocked too. "
            "Use a different approach that doesn't need it, or stop and tell the user what you "
            "need and why."
        )
    hint = {
        "network": "Network access is off for this run. Use the data in the workspace, or tell "
        "the user what you would need to fetch.",
        "risky": "This needs a human to look at it first, and nobody can approve it in this "
        "run. Do it in a form the policy can verify (name the exact files), use another "
        "approach, or tell the user.",
        "forbidden": "This is never allowed for the agent. Rewording the command or doing it "
        "another way (a script, another tool) is blocked too. Choose a different approach, "
        "or stop and tell the user what you needed.",
    }.get(d.kind, "Choose another approach, or tell the user what you need.")
    return f"Blocked by policy ({d.rule}): {d.reason}. {hint}"


def call_key(tool: str, tool_input: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(tool_input, sort_keys=True)
