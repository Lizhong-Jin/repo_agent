"""Never let a developer's installed defaults or credentials affect test behavior."""

import pytest

from cli.config import CONFIG_KEYS


@pytest.fixture(autouse=True)
def isolated_user_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "user-defaults"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    for key in CONFIG_KEYS | {"AGENT_ENV_FILE"}:
        monkeypatch.delenv(key, raising=False)
