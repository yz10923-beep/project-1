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
    # Outside any workspace, so the agent never reads its own (or other runs') logs.
    runs_dir: Path = Path("~/.kama/runs")
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
