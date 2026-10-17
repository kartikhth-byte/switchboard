import os
from pathlib import Path

import pytest

from core.config.settings import export_provider_keys


def test_env_file_key_wins_over_stale_shell_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-from-dotenv\nANTHROPIC_API_KEY=\n")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stale-from-zshrc")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shell")
    sources = export_provider_keys(env)
    assert os.environ["OPENAI_API_KEY"] == "sk-from-dotenv"
    assert sources == {"OPENAI_API_KEY": ".env", "ANTHROPIC_API_KEY": "shell"}


def test_missing_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert set(export_provider_keys(tmp_path / "nope.env").values()) == {"missing"}
