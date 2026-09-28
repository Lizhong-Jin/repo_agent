import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import config_command
from cli.models import ModelSelection
from configuration.environment import CONFIG_KEYS, read_config, save_user_config
from installer.installation import user_config_path

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "config"))
    for key in CONFIG_KEYS | {"AGENT_ENV_FILE"}:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_show_reports_precedence_empty_values_and_masks_keys(tmp_path, monkeypatch, capsys):
    save_user_config(
        {
            "LLM_MODEL": "user-model",
            "LLM_TIMEOUT": "90",
            "LLM_BASE_URL": "https://user.invalid",
            "DEEPSEEK_API_KEY": "private-secret",
        }
    )
    (tmp_path / ".env").write_text("LLM_MODEL=project-model\nLLM_BASE_URL=\n")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    config_command.main(["show"])
    output = capsys.readouterr().out
    assert 'LLM_MODEL = "project-model"    [项目配置]' in output
    assert 'LLM_TIMEOUT = "45"    [环境变量]' in output
    assert 'LLM_BASE_URL = "https://api.deepseek.com"    [项目配置（空值' in output
    assert 'AGENT_MAX_STEPS = "8"    [内置默认]' in output
    assert "private-secret" not in output and "[已设置，已隐藏]" in output
    assert os.environ["LLM_TIMEOUT"] == "45" and "LLM_MODEL" not in os.environ
    config_command.main(["show", "--user"])
    output = capsys.readouterr().out
    assert 'LLM_MODEL = "user-model"    [用户配置]' in output
    assert 'LLM_TIMEOUT = "90"' in output and "project-model" not in output


def test_show_without_config_uses_code_defaults_not_install_template(tmp_path, capsys):
    config_command.main([])
    output = capsys.readouterr().out
    assert 'AGENT_SANDBOX_WRITEBACK = "manual"    [内置默认]' in output
    assert 'LLM_PROVIDER = "deepseek"' in output
    assert not user_config_path().exists()
    assert list(tmp_path.iterdir()) == []


def test_web_search_configuration_and_secret_masking(tmp_path, capsys):
    config_command.main(["set", "AGENT_WEB_SEARCH_PROVIDER", "brave"])
    save_user_config({"BRAVE_SEARCH_API_KEY": "private-search-key"})
    config_command.main(["show"])
    output = capsys.readouterr().out
    assert 'AGENT_WEB_SEARCH_PROVIDER = "brave"' in output
    assert "BRAVE_SEARCH_API_KEY = [已设置，已隐藏]" in output
    assert "private-search-key" not in output
    with pytest.raises(ValueError):
        config_command.validate_value("AGENT_WEB_SEARCH_PROVIDER", "unknown")
    save_user_config({"BRAVE_SEARCH_API_KEY": ""})
    assert any("BRAVE_SEARCH_API_KEY" in warning for warning in
               config_command.validate_configuration(tmp_path))


def test_show_uses_root_and_explicit_project_file(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("LLM_MODEL=root-model\n")
    config_command.main(["show", "--root", str(project)])
    assert "root-model" in capsys.readouterr().out
    custom = tmp_path / "custom.env"
    custom.write_text("LLM_MODEL=custom-model\n")
    monkeypatch.setenv("AGENT_ENV_FILE", str(custom))
    config_command.main(["show", "--root", str(project)])
    output = capsys.readouterr().out
    assert "custom-model" in output and str(custom) in output


@pytest.mark.parametrize(
    "key,value,stored",
    [
        ("LLM_TIMEOUT", "60", "60"),
        ("LLM_STREAM", "FALSE", "false"),
        ("AGENT_WEB_FETCH_ENABLED", "TRUE", "true"),
        ("LLM_PROVIDER", "claude", "anthropic"),
        ("LLM_BASE_URL", "", ""),
        ("LLM_TIMEOUT", "1e-12", "1e-12"),
        ("AGENT_MAX_OUTPUT_TOKENS", "8192", "8192"),
        ("LLM_EXTRA_JSON", '{"option":"value"}', '{"option":"value"}'),
    ],
)
def test_set_updates_only_user_configuration(tmp_path, capsys, key, value, stored):
    save_user_config({"AGENT_MAX_STEPS": "7", "OPENAI_API_KEY": "keep-secret"})
    path = user_config_path()
    path.write_text("# preserve this comment\n" + path.read_text())
    project = tmp_path / ".env"
    project.write_text("LLM_MODEL=project-model\n")
    config_command.main(["set", key, value])
    values = read_config(path)
    assert values[key] == stored and values["AGENT_MAX_STEPS"] == "7"
    assert values["OPENAI_API_KEY"] == "keep-secret"
    assert path.read_text().startswith("# preserve this comment")
    assert path.stat().st_mode & 0o777 == 0o600
    assert project.read_text() == "LLM_MODEL=project-model\n"
    assert "keep-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "key,value",
    [
        ("LLM_TIMEOUT", "0"),
        ("LLM_TIMEOUT", "nan"),
        ("LLM_STREAM", "maybe"),
        ("AGENT_WEB_FETCH_ENABLED", "maybe"),
        ("AGENT_MAX_STEPS", "-1"),
        ("LLM_MAX_RETRIES", "11"),
        ("LLM_TEMPERATURE", "3"),
        ("LLM_EXTRA_JSON", "[]"),
        ("LLM_EXTRA_JSON", '{"n":NaN}'),
        ("LLM_THINKING", "wrong"),
        ("LLM_BASE_URL", "https://host.invalid?api_key=secret"),
        ("AGENT_SANDBOX_WRITEBACK", "bad"),
        ("AGENT_SANDBOX_VERIFY_COMMAND", "echo test"),
    ],
)
def test_invalid_value_leaves_config_unchanged(key, value, capsys):
    save_user_config({"LLM_TIMEOUT": "50"})
    before = user_config_path().read_bytes()
    with pytest.raises(SystemExit) as error:
        config_command.main(["set", key, value])
    assert error.value.code == 1
    assert user_config_path().read_bytes() == before
    assert "secret" not in capsys.readouterr().err


def test_setting_shadowed_by_project_is_explained(tmp_path, capsys):
    (tmp_path / ".env").write_text("LLM_MODEL=project-model\n")
    config_command.main(["set", "LLM_MODEL", "user-model"])
    assert "仍被项目配置覆盖" in capsys.readouterr().out
    assert read_config(user_config_path())["LLM_MODEL"] == "user-model"


def test_interactive_edit_and_hidden_secret_input(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    choices = iter(["LLM_TIMEOUT", "75"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(choices))
    config_command.main(["edit"])
    assert read_config(user_config_path())["LLM_TIMEOUT"] == "75"
    monkeypatch.setattr("cli.config_command.getpass.getpass", lambda prompt: "hidden-secret")
    config_command.main(["set", "DEEPSEEK_API_KEY"])
    assert read_config(user_config_path())["DEEPSEEK_API_KEY"] == "hidden-secret"
    assert "hidden-secret" not in capsys.readouterr().out
    config_command.main(["show", "--user"])
    assert "hidden-secret" not in capsys.readouterr().out


def test_cancel_or_plain_secret_argument_never_saves(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    def cancel(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", cancel)
    with pytest.raises(SystemExit) as error:
        config_command.main(["edit", "LLM_MODEL"])
    assert error.value.code == 0 and not user_config_path().exists()
    with pytest.raises(SystemExit):
        config_command.main(["set", "DEEPSEEK_API_KEY", "secret-on-command-line"])
    output = capsys.readouterr()
    assert "secret-on-command-line" not in output.out + output.err
    assert not user_config_path().exists()


def test_config_entry_bypasses_agent_and_broken_runtime_values(tmp_path, monkeypatch, capsys):
    from cli import main as cli

    monkeypatch.setattr(sys, "argv", ["repo-agent", "config", "show"])
    monkeypatch.setenv("LLM_STREAM", "invalid")
    monkeypatch.setattr("cli.runtime_setup.LLMClient", lambda *a, **kw: pytest.fail("must not start model"))
    monkeypatch.setattr(
        "cli.execution_environment.detect_environment", lambda **kw: pytest.fail("must not start Docker")
    )
    cli.main()
    assert 'LLM_STREAM = "invalid"' in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_config_model_sets_values_without_agent(monkeypatch, capsys):
    monkeypatch.setattr(
        config_command,
        "prompt_model",
        lambda wizard: ModelSelection("qwen", "qwen-plus", "fake-key"),
    )
    config_command.main(["model"])
    assert read_config(user_config_path())["DASHSCOPE_API_KEY"] == "fake-key"
    assert "fake-key" not in capsys.readouterr().out


def test_installed_entry_can_set_show_and_get_path(tmp_path):
    command = [str(SOURCE / ".venv/bin/repo-agent"), "config"]
    env = {**os.environ, "PYTHONPATH": str(SOURCE)}
    for args in [["set", "AGENT_MAX_STEPS", "13"], ["show"], ["path"]]:
        result = subprocess.run(
            command + args, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10
        )
        assert result.returncode == 0, result.stderr
        if args == ["show"]:
            assert 'AGENT_MAX_STEPS = "13"' in result.stdout
        if args == ["path"]:
            assert result.stdout.strip() == str(user_config_path())
    assert not (tmp_path / "logs").exists()
