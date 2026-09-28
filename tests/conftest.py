"""Never let a developer's installed defaults or credentials affect test behavior."""

import os

import pytest

from configuration.environment import CONFIG_KEYS


@pytest.fixture(autouse=True)
def isolated_user_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "user-defaults"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    for key in CONFIG_KEYS | {"AGENT_ENV_FILE"}:
        monkeypatch.delenv(key, raising=False)
    # Installer settings are not model configuration, but still affect default tests.
    for key in tuple(os.environ):
        if key.startswith("AGENT_INSTALL_") or key in {"PIP_RETRIES", "npm_config_fetch_retries"}:
            monkeypatch.delenv(key, raising=False)
