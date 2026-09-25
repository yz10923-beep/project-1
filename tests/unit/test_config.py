from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.config import ConfigError, load_settings


def test_defaults() -> None:
    s = load_settings(env={}, dotenv_path=None)
    assert (s.host, s.port, s.log_level) == ("127.0.0.1", 7437, "INFO")


def test_env_overrides_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("KAMA_PORT=9000\nKAMA_HOST=0.0.0.0\n")
    s = load_settings(env={"KAMA_PORT": "9001"}, dotenv_path=dotenv)
    assert s.port == 9001  # env wins
    assert s.host == "0.0.0.0"  # dotenv fills what env does not set


def test_log_level_is_case_insensitive() -> None:
    assert load_settings(env={"KAMA_LOG_LEVEL": "debug"}, dotenv_path=None).log_level == "DEBUG"


def test_unrelated_env_vars_are_ignored() -> None:
    s = load_settings(env={"KAMA_NOT_A_SETTING": "x", "PATH": "/bin"}, dotenv_path=None)
    assert s.port == 7437


@pytest.mark.parametrize("port", ["abc", "70000", "-1"])
def test_bad_port_is_a_config_error(port: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(env={"KAMA_PORT": port}, dotenv_path=None)
