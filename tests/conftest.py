"""Never let a developer's installed defaults or credentials affect test behavior."""

import os

import pytest
from session_helpers import Model

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from cli.session_status import SessionStatus
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


@pytest.fixture
def open_conversation(tmp_path):
    stores = []

    def create(project=None, *, new=False, model=None):
        project = project or tmp_path / "project"
        project.mkdir(exist_ok=True)
        store = SessionStore(project, new=new).open()
        stores.append(store)
        model = model or Model()
        status = SessionStatus(project, context_window=1000)
        runtime = AgentRuntime(model, on_event=status)
        conversation = SavedConversation(store, runtime, model.config, status)
        conversation.checkpoint(strict=True)
        return conversation

    yield create
    for store in stores:
        store.close()
