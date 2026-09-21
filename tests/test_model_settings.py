import asyncio
import json
import os
import sys
from types import SimpleNamespace

import httpx
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent import AgentRuntime
from agent.Tracing import Tracer
from cli.config import CONFIG_KEYS, configured_environment, read_config, save_user_config
from cli.installation import user_config_path
from cli.live import SessionStatus
from cli.models import ModelControl, ModelSelection, ModelWizard, prompt_model
from cli.tui import ConversationUI
from llm import LLMClient, LLMConfig
from llm.providers import PROVIDERS


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "config"))
    for key in CONFIG_KEYS | {"AGENT_ENV_FILE"}:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wizard_uses_supported_providers_and_manual_models(provider):
    wizard = ModelWizard("deepseek", "old", base_url="https://old.invalid")
    assert all(name in wizard.prompt() for name in PROVIDERS)
    wizard.submit(str(list(PROVIDERS).index(provider) + 1))
    wizard.submit("my-model-id")
    assert wizard.secret
    selection = wizard.submit("new-secret")
    assert (selection.provider, selection.model, selection.api_key) == (
        provider,
        "my-model-id",
        "new-secret",
    )
    assert selection.base_url == ("https://old.invalid" if provider == "deepseek" else None)
    assert "new-secret" not in repr(selection)


def test_wizard_reuses_saved_key_without_revealing_it():
    save_user_config({"DASHSCOPE_API_KEY": "saved-secret"})
    wizard = ModelWizard("deepseek", "old")
    with pytest.raises(ValueError):
        wizard.submit("unsupported")
    wizard.submit("qwen")
    with pytest.raises(ValueError):
        wizard.submit("")
    wizard.submit("q-model")
    assert "saved-secret" not in wizard.prompt()
    assert wizard.submit("").api_key == "saved-secret"


def test_save_preserves_comments_other_keys_and_private_permissions():
    path = user_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        "# comment\nLLM_MODEL=old\nexport LLM_MODEL=duplicate\nOPENAI_API_KEY=keep\nAGENT_MAX_STEPS=17\n"
    )
    save_user_config({"LLM_MODEL": "new", "DEEPSEEK_API_KEY": 'key$literal"quote'})
    assert "# comment" in path.read_text()
    assert path.read_text().count("LLM_MODEL=") == 1
    values = read_config(path)
    assert values["OPENAI_API_KEY"] == "keep" and values["AGENT_MAX_STEPS"] == "17"
    assert values["DEEPSEEK_API_KEY"] == 'key$literal"quote'
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "replace_failure", "control_character"])
def test_failed_save_preserves_original_config(tmp_path, monkeypatch, kind):
    save_user_config({"LLM_MODEL": "old"})
    path = user_config_path()
    before = path.read_bytes()
    if kind == "symlink":
        other = tmp_path / "outside"
        path.rename(other)
        path.symlink_to(other)
    elif kind == "replace_failure":

        def fail(*args):
            raise OSError("failed")

        monkeypatch.setattr("cli.config.os.replace", fail)
    with pytest.raises((ValueError, OSError)):
        save_user_config({"LLM_MODEL": "new\nINJECT=1" if kind == "control_character" else "new"})
    assert path.read_bytes() == before
    assert list(path.parent.glob(".model-settings-*")) == []


def test_defaults_file_is_not_loaded(tmp_path):
    save_user_config({"LLM_MODEL": "user-model", "DEEPSEEK_API_KEY": "user-key"})
    (tmp_path / ".env.defaults").write_text("LLM_MODEL=ignored")
    with configured_environment(tmp_path):
        assert os.environ["LLM_MODEL"] == "user-model"
        assert os.environ["DEEPSEEK_API_KEY"] == "user-key"


def test_prompt_uses_hidden_input_and_can_cancel(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    answers = iter(["deepseek", "manual-model"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("cli.models.getpass.getpass", lambda prompt: "hidden-secret")
    assert prompt_model(ModelWizard("deepseek", None)).api_key == "hidden-secret"
    assert "hidden-secret" not in capsys.readouterr().out
    assert not user_config_path().exists()

    def cancel(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    with pytest.raises(KeyboardInterrupt):
        prompt_model(ModelWizard("deepseek", None))
    assert not user_config_path().exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_startup_wizard_saves_and_uses_new_model(tmp_path, monkeypatch, explicit):
    from cli import main as cli

    monkeypatch.setattr(LLMClient, "get_context_limit", lambda self, **kw: None)
    if explicit:
        save_user_config(
            {"LLM_PROVIDER": "deepseek", "LLM_MODEL": "old", "DEEPSEEK_API_KEY": "old-key"}
        )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    args = ["repo-agent", "--sandbox", "local", "--root", str(tmp_path)]
    if explicit:
        args.append("--configure-model")
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(
        cli, "prompt_model", lambda wizard: ModelSelection("qwen", "chosen", "new-key")
    )
    seen = []

    def interactive(runtime, **kwargs):
        seen.append(runtime.llm.config)
        assert kwargs["models"].config.model == "chosen"

    monkeypatch.setattr(cli, "run_interactive", interactive)
    cli.main()
    assert seen[0].model == "chosen" and seen[0].api_key == "new-key"
    values = read_config(user_config_path())
    assert values["LLM_PROVIDER"] == "qwen" and values["DASHSCOPE_API_KEY"] == "new-key"
    if explicit:
        assert values["DEEPSEEK_API_KEY"] == "old-key"


def test_startup_cancel_does_not_create_sandbox_or_save(tmp_path, monkeypatch):
    from cli import main as cli

    monkeypatch.setattr(sys, "argv", ["repo-agent", "--configure-model", "--root", str(tmp_path)])

    def cancel(wizard):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "prompt_model", cancel)
    monkeypatch.setattr(cli, "detect_environment", lambda **kw: pytest.fail("must configure first"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    assert not user_config_path().exists()


def make_control(tmp_path, monkeypatch):
    calls, clients = [], []

    def factory(config):
        def response(request):
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{"id": config.model, "context_length": 2000}]
                })
            calls.append(
                (str(request.url), request.headers["authorization"], json.loads(request.content))
            )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        client = LLMClient(
            config, http_client=httpx.Client(transport=httpx.MockTransport(response))
        )
        client._owned = True
        clients.append(client)
        return client

    config = LLMConfig(
        "deepseek", "old", api_key="old-key", base_url="https://old.invalid", stream=False
    )
    runtime = AgentRuntime(factory(config), request_extra={"thinking": {"type": "enabled"}})
    thinking = SimpleNamespace(provider="deepseek", model="old", base={"native": 1}, current={})
    status = SessionStatus(tmp_path, context_window=1000)
    control = ModelControl(
        runtime, config, thinking=thinking, status=status, client_factory=factory
    )
    return control, calls, clients


def test_switch_changes_wire_request_resets_settings_and_logs_no_key(tmp_path, monkeypatch):
    control, calls, clients = make_control(tmp_path, monkeypatch)
    with Tracer(tmp_path / "traces", provider="deepseek", model="old") as tracer:
        control.tracer = tracer
        control.runtime.on_event = tracer
        control.runtime.run("first")
        control.switch(ModelSelection("qwen", "new", "new-key"))
        control.runtime.run("second")
    assert clients[0]._http.is_closed
    assert calls[1][0].startswith(PROVIDERS["qwen"].base_url)
    assert calls[1][1] == "Bearer new-key"
    assert calls[1][2]["model"] == "new" and "thinking" not in calls[1][2]
    assert control.thinking.provider == "qwen" and control.thinking.current["mode"] == "auto"
    assert control.status.context_window == 2000
    assert "服务端自动获取" in control.status.describe_context()
    log = tracer.jsonl_path.read_text()
    assert '"event": "model_changed"' in log
    assert '"model": "new"' in log and "new-key" not in log and "old-key" not in log
    control.close()
    assert clients[1]._http.is_closed


def test_failed_switch_preserves_client_settings_and_configuration(tmp_path, monkeypatch):
    control, _, clients = make_control(tmp_path, monkeypatch)
    save_user_config({"LLM_MODEL": "old"})
    before = user_config_path().read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("cli.config.os.replace", fail)
    with pytest.raises(OSError):
        control.switch(ModelSelection("qwen", "new", "new-key"))
    assert control.runtime.llm is clients[0] and not clients[0]._http.is_closed
    assert clients[1]._http.is_closed
    assert control.config.model == "old" and control.runtime.request_extra
    assert user_config_path().read_bytes() == before
    clients[0].close()


def test_full_terminal_model_wizard_masks_key_and_clears_history(tmp_path, monkeypatch):
    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 3)

    async def run():
        control, calls, clients = make_control(tmp_path, monkeypatch)
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                control.runtime,
                models=control,
                status=control.status,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            running = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("first\r")
            await until(lambda: bool(ui.history) and not ui.busy)
            history = ui.history
            pipe.send_text("/model\r")
            await until(lambda: ui.model_wizard is not None)
            pipe.send_text("qwen\r")
            await until(lambda: ui.model_wizard.stage == "model")
            pipe.send_text("new\r")
            await until(lambda: ui.model_wizard.secret)
            pipe.send_text("private-key")
            await until(lambda: ui.editor.text == "private-key")
            rendered = ui.editor.control.create_content(width=80, height=3).get_line(0)
            assert "private-key" not in "".join(text for _, text, *rest in rendered)
            assert "*" * len("private-key") in "".join(text for _, text, *rest in rendered)
            assert "private-key" not in ui.transcript
            pipe.send_text("\x03")
            await until(lambda: ui.model_wizard is None)
            assert ui.history == history and control.config.model == "old"
            assert not ui.editor.text and not user_config_path().exists()
            pipe.send_text("/model\r")
            await until(lambda: ui.model_wizard is not None)
            pipe.send_text("qwen\rnew\rprivate-key\r")
            await until(lambda: control.config.model == "new")
            assert not ui.history and "qwen / new" in ui.footer_text
            assert "private-key" not in ui.transcript
            pipe.send_text("second\r")
            await until(lambda: len(calls) == 2 and not ui.busy)
            assert [m["role"] for m in calls[1][2]["messages"]] == ["system", "user"]
            pipe.send_text("/exit\r")
            await asyncio.wait_for(running, 3)
        control.close()

    asyncio.run(run())


def test_plain_session_switch_discards_history_and_reuses_new_client(tmp_path, monkeypatch):
    from cli.interactive import run_interactive

    control, calls, clients = make_control(tmp_path, monkeypatch)
    answers = iter(["first", "/model", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(
        "cli.models.prompt_model", lambda wizard: ModelSelection("qwen", "new", "new-key")
    )
    run_interactive(control.runtime, models=control)
    assert calls[0][2]["model"] == "old" and calls[1][2]["model"] == "new"
    assert [m["role"] for m in calls[1][2]["messages"]] == ["system", "user"]
    control.close()


def test_model_command_waits_for_running_task(tmp_path, monkeypatch):
    control, _, clients = make_control(tmp_path, monkeypatch)
    with create_pipe_input() as pipe:
        ui = ConversationUI(
            control.runtime, models=control, terminal_input=pipe, terminal_output=DummyOutput()
        )
        ui.busy = True
        ui.editor.text = "/model"
        ui.submit()
        assert ui.model_wizard is None and ui.editor.text == "/model"
        assert control.config.model == "old"
    clients[0].close()
