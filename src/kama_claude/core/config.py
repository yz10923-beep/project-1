"""Settings. Priority (low -> high): built-in defaults -> .env -> process env vars.

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
    runs_dir: Path = Path(".kama/runs")
    anthropic_api_key: SecretStr | None = None


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


def load_settings(
    env: Mapping[str, str] | None = None, dotenv_path: Path | None = Path(".env")
) -> Settings:
    """Merge .env and environment variables over defaults. Raises ConfigError on bad values."""
    merged: dict[str, str] = {}
    if dotenv_path is not None and dotenv_path.is_file():
        merged |= _from_env(dotenv_values(dotenv_path))
    merged |= _from_env(os.environ if env is None else env)
    try:
        return Settings.model_validate(merged)
    except ValidationError as e:
        raise ConfigError(str(e)) from e
