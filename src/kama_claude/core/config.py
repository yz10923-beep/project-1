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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ENV_PREFIX = "KAMA_"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=7437, ge=0, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class ConfigError(Exception):
    pass


def _from_env(env: Mapping[str, str | None]) -> dict[str, str]:
    fields = Settings.model_fields
    out: dict[str, str] = {}
    for key, value in env.items():
        if not key.startswith(ENV_PREFIX) or value is None:
            continue
        name = key.removeprefix(ENV_PREFIX).lower()
        if name in fields:
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
