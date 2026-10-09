"""Settings. Priority (low -> high): built-in defaults -> ~/.kama/.env -> ./.env ->
process env vars.

~/.kama/.env is the home for secrets like the API key, so `kama` finds them from any
workspace directory. ./.env (current directory) overrides it per project.

A config file (~/.kama/config.toml) is deliberately deferred until a setting
needs it; env vars are enough for one daemon on one machine.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

ENV_PREFIX = "KAMA_"
# Read from .env too, so the key can live there instead of the shell profile.
_UNPREFIXED = {"ANTHROPIC_API_KEY": "anthropic_api_key"}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # daemon
    host: str = "127.0.0.1"
    port: int = Field(default=7437, ge=0, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # agent
    model: str = "claude-opus-5"
    max_tokens: int = Field(default=16_000, ge=1)
    max_steps: int = Field(default=30, ge=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    refusal_fallback: bool = True
    # S3: offer the task_* tools and nudge the model when it stops with open tasks.
    planning: bool = True
    # S4: runs in a session continue its history and get notes; false = every run starts
    # from nothing (the A/B baseline for memory).
    memory: bool = True
    sessions_dir: Path = Path("~/.kama/sessions")
    memory_dir: Path = Path("~/.kama/memory")  # durable notes, per workspace
    # Outside any workspace, so the agent never reads its own (or other runs') logs.
    runs_dir: Path = Path("~/.kama/runs")
    # S5: every tool call goes through the permission policy (false = the S4 approvals:
    # ask for bash/write_file, -y approves everything), and bash runs in the OS sandbox
    # (auto = the best backend that works here; off = run directly).
    policy: bool = True
    policy_file: Path = Path("~/.kama/policy.toml")
    sandbox: Literal["auto", "bwrap", "unshare", "off"] = "auto"
    # Environment variables kept for bash although they look like credentials (comma list).
    bash_env_keep: str = ""
    # More paths no tool call may read or change, like the daemon's own files (comma
    # list). The eval harness puts its graders here; bwrap also hides them from bash.
    private_paths: str = ""
    # Model calls that fail with a retryable error (429, 529, 5xx, network) are retried
    # with backoff, up to this many times and this much total waiting per call.
    llm_max_retries: int = Field(default=4, ge=0, le=20)
    llm_retry_budget_s: float = Field(default=120, ge=0)
    # S6: context governance. Tool results are capped when created (the full output is
    # kept outside the workspace), and the history is compacted server-side when the next
    # request would exceed the budget (tokens). false = the S5 agent, byte for byte.
    context: bool = True
    context_budget: int = Field(default=120_000, ge=10_000)
    tool_result_max_chars: int = Field(default=30_000, ge=2_000)
    # S7: extensions. Each switch, off, removes one thing from the request, so an A/B
    # changes one thing; all four off = the S6 agent, byte for byte.
    mcp: bool = True  # tools from the MCP servers in mcp_file
    mcp_file: Path = Path("~/.kama/mcp.toml")
    skills: bool = True  # the skill index and load_skill (skills_dir + workspace .kama/skills)
    skills_dir: Path = Path("~/.kama/skills")
    subagents: bool = True  # delegate: child runs with their own context
    tool_search: bool = False  # MCP tools deferred behind tool search (measured, not default)
    anthropic_api_key: SecretStr | None = None

    # daemon: auth token file (mode 0600), and how long a run waits for a human approval
    token_file: Path = Path("~/.kama/core.token")
    approval_timeout_s: float = Field(default=600, gt=0)


class ConfigError(Exception):
    pass


def _from_env(env: Mapping[str, str | None]) -> dict[str, str]:
    fields = Settings.model_fields
    out: dict[str, str] = {}
    for key, value in env.items():
        if not value:  # unset and empty both mean "use the default"
            continue
        if key in _UNPREFIXED:
            out[_UNPREFIXED[key]] = value
            continue
        if not key.startswith(ENV_PREFIX):
            continue
        name = key.removeprefix(ENV_PREFIX).lower()
        if name in fields and name != "anthropic_api_key":
            out[name] = value.upper() if name == "log_level" else value
    return out


def default_dotenv_paths() -> list[Path]:
    """Lowest priority first."""
    return [Path.home() / ".kama" / ".env", Path(".env")]


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return _from_env(dotenv_values(path, encoding="utf-8-sig"))  # -sig: tolerate a BOM
    except UnicodeDecodeError as e:
        # Typically `echo ... > .env` in Windows PowerShell 5.1, which writes UTF-16.
        raise ConfigError(f"{path} is not UTF-8 text; re-save it as UTF-8") from e


def load_settings(
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | list[Path] | None = None,
    *,
    use_default_dotenv: bool = True,
) -> Settings:
    """Merge .env files and environment variables over defaults.

    `dotenv_path` replaces the default .env locations (tests pass explicit files);
    `use_default_dotenv=False` with no path reads no .env at all.
    Raises ConfigError on unreadable files or invalid values.
    """
    if dotenv_path is None:
        paths = default_dotenv_paths() if use_default_dotenv else []
    else:
        paths = dotenv_path if isinstance(dotenv_path, list) else [dotenv_path]
    merged: dict[str, str] = {}
    for path in paths:
        if path.is_file():
            merged |= _read_dotenv(path)
    merged |= _from_env(os.environ if env is None else env)
    try:
        return Settings.model_validate(merged)
    except ValidationError as e:
        raise ConfigError(str(e)) from e
