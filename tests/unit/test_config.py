from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.config import ConfigError, load_settings


def test_defaults() -> None:
    s = load_settings(env={}, use_default_dotenv=False)
    assert (s.host, s.port, s.log_level) == ("127.0.0.1", 7437, "INFO")


def test_env_overrides_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("KAMA_PORT=9000\nKAMA_HOST=0.0.0.0\n")
    s = load_settings(env={"KAMA_PORT": "9001"}, dotenv_path=dotenv)
    assert s.port == 9001  # env wins
    assert s.host == "0.0.0.0"  # dotenv fills what env does not set


def test_log_level_is_case_insensitive() -> None:
    assert (
        load_settings(env={"KAMA_LOG_LEVEL": "debug"}, use_default_dotenv=False).log_level
        == "DEBUG"
    )


def test_unrelated_env_vars_are_ignored() -> None:
    s = load_settings(env={"KAMA_NOT_A_SETTING": "x", "PATH": "/bin"}, use_default_dotenv=False)
    assert s.port == 7437


@pytest.mark.parametrize("port", ["abc", "70000", "-1"])
def test_bad_port_is_a_config_error(port: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(env={"KAMA_PORT": port}, use_default_dotenv=False)


def test_agent_defaults_and_overrides() -> None:
    s = load_settings(env={}, use_default_dotenv=False)
    assert (s.model, s.max_steps, s.effort, s.refusal_fallback) == ("claude-opus-5", 30, None, True)
    s = load_settings(
        env={
            "KAMA_MODEL": "claude-sonnet-5",
            "KAMA_EFFORT": "low",
            "KAMA_REFUSAL_FALLBACK": "false",
        },
        use_default_dotenv=False,
    )
    assert (s.model, s.effort, s.refusal_fallback) == ("claude-sonnet-5", "low", False)


def test_api_key_read_from_dotenv_and_hidden_in_repr(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("ANTHROPIC_API_KEY=sk-test-123\n")
    s = load_settings(env={}, dotenv_path=dotenv)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-test-123"
    assert "sk-test-123" not in repr(s)


def test_empty_values_fall_back_to_defaults() -> None:
    assert load_settings(env={"KAMA_EFFORT": ""}, use_default_dotenv=False).effort is None


def test_bad_effort_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        load_settings(env={"KAMA_EFFORT": "extreme"}, use_default_dotenv=False)


def test_cwd_dotenv_overrides_home_dotenv(tmp_path: Path) -> None:
    home, local = tmp_path / "home.env", tmp_path / "local.env"
    home.write_text("ANTHROPIC_API_KEY=from-home\nKAMA_MODEL=m-home\n")
    local.write_text("KAMA_MODEL=m-local\n")
    s = load_settings(env={}, dotenv_path=[home, local])
    assert s.model == "m-local"
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "from-home"


def test_default_dotenv_paths_include_home_then_cwd() -> None:
    from kama_claude.core.config import default_dotenv_paths

    home, cwd = default_dotenv_paths()
    assert home == Path.home() / ".kama" / ".env" and cwd == Path(".env")


@pytest.mark.parametrize(
    "data",
    [
        b"ANTHROPIC_API_KEY=k\r\n",
        "\ufeffANTHROPIC_API_KEY=k\n".encode(),
        b'ANTHROPIC_API_KEY="k"\n',
    ],
)
def test_windows_style_dotenv_files_load(tmp_path: Path, data: bytes) -> None:
    p = tmp_path / ".env"
    p.write_bytes(data)
    key = load_settings(env={}, dotenv_path=p).anthropic_api_key
    assert key is not None and key.get_secret_value() == "k"


def test_utf16_dotenv_is_a_clear_config_error(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_bytes("ANTHROPIC_API_KEY=k\r\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="UTF-8"):
        load_settings(env={}, dotenv_path=p)
