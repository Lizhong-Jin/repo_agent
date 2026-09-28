"""Installation and configuration contracts across independent workspaces."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from configuration.environment import (
    CONFIG_KEYS,
    configured_environment,
    read_config,
    user_config_path,
)
from installer.setup import configure_path, configure_user, install_command

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "user home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHONPATH", str(SOURCE))
    for key in CONFIG_KEYS | {
        "AGENT_ENV_FILE",
        "AGENT_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "ZDOTDIR",
    }:
        monkeypatch.delenv(key, raising=False)
    return home


def test_configuration_precedence_literals_and_environment_restoration(user_home, tmp_path):
    config = user_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(
        "LLM_MODEL=user-model\nLLM_BASE_URL=https://global.invalid\nAGENT_MAX_STEPS=12\n"
        "DEEPSEEK_API_KEY=global-key\nPATH=ignored\n"
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "LLM_MODEL=$(touch never-created)\nLLM_BASE_URL=\nDEEPSEEK_API_KEY=\n"
    )
    os.environ["AGENT_MAX_STEPS"] = "20"
    try:
        with configured_environment(project):
            assert os.environ["LLM_MODEL"] == "$(touch never-created)"
            assert os.environ["LLM_BASE_URL"] == ""
            assert os.environ["DEEPSEEK_API_KEY"] == ""
            assert os.environ["AGENT_MAX_STEPS"] == "20"
            assert os.environ["PATH"] != "ignored"
        assert "LLM_MODEL" not in os.environ
        assert not (project / "never-created").exists()
    finally:
        os.environ.pop("AGENT_MAX_STEPS")


def test_custom_config_and_private_errors(user_home, tmp_path, monkeypatch):
    config = tmp_path / "custom.env"
    monkeypatch.setenv("AGENT_ENV_FILE", str(config))
    with pytest.raises(FileNotFoundError):
        with configured_environment(tmp_path):
            pass
    config.write_text("DEEPSEEK_API_KEY='private-value\n")
    with pytest.raises(ValueError) as error:
        read_config(config)
    assert "private-value" not in str(error.value)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert user_config_path() == tmp_path / "xdg/repo-agent/.env"
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "override"))
    assert user_config_path() == tmp_path / "override/.env"


def test_user_setup_creates_private_template_without_importing_environment(user_home, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "kimi")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("MOONSHOT_API_KEY", "environment-secret")
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("Installation must not prompt"))
    config = configure_user(SOURCE)
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.read_bytes() == (SOURCE / ".env.example").read_bytes()
    values = read_config(config)
    assert values["LLM_MODEL"] == ""
    assert (values["LLM_PROVIDER"] or "deepseek") == "deepseek"
    assert all(not value for name, value in values.items() if name.endswith("_API_KEY"))
    assert "environment-secret" not in config.read_text()


@pytest.mark.parametrize(
    "content",
    ["", "LLM_MODEL=\n", "LLM_MODEL=mine\nDEEPSEEK_API_KEY=secret\n", "unfinished config"],
)
def test_reinstall_preserves_existing_config_even_if_incomplete(user_home, content):
    config = configure_user(SOURCE)
    config.write_text(content)
    configure_user(SOURCE)
    assert config.read_text() == content


@pytest.mark.parametrize("shell", ["zsh", "bash"])
@pytest.mark.parametrize("name", ["repo-agent", "repo-agent-build-sandbox"])
def test_command_and_path_setup_preserves_existing_files(
    user_home, tmp_path, monkeypatch, shell, name
):
    install = tmp_path / "agent's install"
    entry = install / ".venv/bin" / name
    entry.parent.mkdir(parents=True)
    entry.write_text("entry")
    bin_dir = user_home / ".local/bin"
    command = install_command(install, bin_dir, name)
    assert command.resolve() == entry
    assert install_command(install, bin_dir, name) == command
    monkeypatch.setenv("SHELL", f"/bin/{shell}")
    rc = user_home / f".{shell}rc"
    rc.write_text("# existing setup\n")
    files = configure_path(bin_dir)
    previous = {path: path.read_text() for path in files}
    configure_path(bin_dir)
    assert {path: path.read_text() for path in files} == previous
    assert rc.read_text().startswith("# existing setup\n")
    command.unlink()
    command.write_text("another tool")
    with pytest.raises(ValueError, match="保留已有命令"):
        install_command(install, bin_dir, name)
    assert command.read_text() == "another tool"


@pytest.mark.parametrize("explicit_root", [False, True])
def test_installed_command_uses_workspace_and_global_credentials(
    user_home, tmp_path, monkeypatch, explicit_root
):
    config = configure_user(SOURCE)
    config.write_text(
        config.read_text()
        .replace("LLM_MODEL=\n", "LLM_MODEL=global-model\n")
        .replace("DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY=fake-key-never-sent\n")
    )
    project = tmp_path / "working project"
    project.mkdir()
    (project / ".env").write_text("LLM_MODEL=project-model\n")
    entry = SOURCE / ".venv/bin/repo-agent"
    args = [str(entry), "--sandbox", "local", "--sandbox-writeback", "manual"]
    if explicit_root:
        args.extend(["--root", str(project)])
    result = subprocess.run(
        args,
        cwd=tmp_path if explicit_root else project,
        input="/exit\n",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    metadata = json.loads(
        next((project / "logs").glob("*.trace.jsonl")).read_text().splitlines()[0]
    )
    assert metadata["workspace"] == str(project)
    assert metadata["model"] == "project-model"
    assert "fake-key-never-sent" not in result.stdout + result.stderr
    assert not (project / ".venv").exists()
    assert not (tmp_path / "logs").exists()


def test_help_does_not_require_valid_config(user_home, tmp_path):
    (tmp_path / ".env").write_text("broken config")
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", "--root", str(tmp_path), "--help"],
        cwd=SOURCE,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--root" in result.stdout


def test_setup_installs_working_command_without_modifying_projects(
    user_home, tmp_path, monkeypatch
):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    install = tmp_path / "isolated install"
    (install / ".venv/bin").mkdir(parents=True)
    (install / ".env.example").write_bytes((SOURCE / ".env.example").read_bytes())
    for name in ("repo-agent", "repo-agent-build-sandbox"):
        (install / ".venv/bin" / name).symlink_to(SOURCE / ".venv/bin" / name)
    project = tmp_path / "empty project"
    project.mkdir()
    setup = [
        sys.executable,
        "-m",
        "installer.setup",
        "--agent-home",
        str(install),
        "--mode",
        "local",
        "--skip-sandbox",
    ]
    for _ in range(2):
        result = subprocess.run(
            setup, cwd=project, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15
        )
        assert result.returncode == 0, result.stderr
        assert str(user_config_path()) in result.stdout
        assert "首次启动前" in result.stdout
    assert list(project.iterdir()) == []
    for name in ("repo-agent", "repo-agent-build-sandbox"):
        assert (user_home / ".local/bin" / name).resolve() == SOURCE / ".venv/bin" / name
    config = user_config_path()
    assert read_config(config)["LLM_MODEL"] == ""
    config.write_text(
        config.read_text()
        .replace("LLM_MODEL=\n", "LLM_MODEL=global-model\n")
        .replace("DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY=fake-key-never-sent\n")
    )
    result = subprocess.run(
        [
            str(user_home / ".local/bin/repo-agent"),
            "--sandbox",
            "local",
            "--sandbox-writeback",
            "manual",
        ],
        cwd=project,
        input="/exit\n",
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    metadata = json.loads(
        next((project / "logs").glob("*.trace.jsonl")).read_text().splitlines()[0]
    )
    assert metadata["model"] == "global-model"
    assert metadata["workspace"] == str(project)


def test_failed_sandbox_build_reports_failure_before_path_setup(user_home, monkeypatch):
    from installer import setup

    install = user_home / "isolated install"
    install.mkdir()
    (install / ".env.example").write_bytes((SOURCE / ".env.example").read_bytes())
    monkeypatch.setattr(sys, "argv", ["setup", "--agent-home", str(install), "--mode", "docker"])

    def failed_build(command, **kwargs):
        assert command == [sys.executable, "-I", "-m", "sandbox.build"]
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(setup.subprocess, "run", failed_build)
    with pytest.raises(SystemExit) as error:
        setup.main()
    assert error.value.code == 1
    assert user_config_path().exists()  # The template survives so the install can be retried.
    assert not (user_home / ".local/bin/repo-agent").exists()
    assert not (user_home / ".zshrc").exists()


@pytest.mark.parametrize(
    "flags,provider,model,steps,base_url",
    [
        ([], "deepseek", "configured-model", 12, "https://configured.invalid/v1"),
        (["读取 README"], "deepseek", "configured-model", 12, "https://configured.invalid/v1"),
        (
            ["--model", "override-model", "--max-steps", "3"],
            "deepseek",
            "override-model",
            3,
            "https://configured.invalid/v1",
        ),
        (
            ["--provider", "qwen", "--base-url", "https://override.invalid/v1"],
            "qwen",
            "configured-model",
            12,
            "https://override.invalid/v1",
        ),
    ],
)
def test_startup_defaults_and_partial_cli_overrides(
    user_home, tmp_path, monkeypatch, flags, provider, model, steps, base_url
):
    from types import SimpleNamespace

    import httpx

    from cli import main as cli
    from llm import LLMClient

    config = configure_user(SOURCE)
    config.write_text(
        "LLM_PROVIDER=deepseek\nLLM_MODEL=configured-model\n"
        "DEEPSEEK_API_KEY=deepseek-test-key\nDASHSCOPE_API_KEY=qwen-test-key\n"
        "LLM_BASE_URL=https://configured.invalid/v1\nLLM_TIMEOUT=45\nLLM_STREAM=false\n"
        "LLM_TEMPERATURE=0.3\nLLM_THINKING=disabled\nLLM_CONTEXT_WINDOW=65536\n"
        "AGENT_MAX_STEPS=12\nAGENT_MAX_OUTPUT_TOKENS=2048\n"
    )
    before = config.read_bytes()
    project = tmp_path / "project with no local config"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["repo-agent", *flags])
    # Model defaults must also work with zero CLI arguments and the default sandbox.
    sandbox = SimpleNamespace(healthy=True, tools=lambda: [], close=lambda: None,
                              execution_context=lambda: {"gpu_access": {"enabled": False}})
    monkeypatch.setattr("cli.execution_environment.NativeBackend", lambda *args, **kwargs: sandbox)
    monkeypatch.setattr("cli.execution_environment.detect_environment", lambda **kwargs: pytest.fail("Docker selected"))
    seen_clients = []
    seen_runtime = []

    def response(request):
        assert request.headers["Authorization"] == f"Bearer {provider}-test-key"
        body = json.loads(request.content)
        assert body["model"] == model
        assert body["max_tokens"] == 2048
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ]
            },
        )

    def client_from_config(settings):
        client = LLMClient(
            settings, http_client=httpx.Client(transport=httpx.MockTransport(response))
        )
        seen_clients.append(client)
        return client

    def interactive(runtime, **kwargs):
        seen_runtime.append(runtime)
        assert kwargs["conversation"].mode == "native"
        assert runtime.run("mock task").status == "completed"

    monkeypatch.setattr("cli.runtime_setup.LLMClient", client_from_config)
    monkeypatch.setattr("cli.application.run_interactive", interactive)
    cli.main()
    assert len(seen_clients) == 1
    settings = seen_clients[0].config
    assert (settings.provider, settings.model, settings.base_url) == (provider, model, base_url)
    assert settings.timeout == 45
    assert settings.stream is False
    if flags != ["读取 README"]:
        assert len(seen_runtime) == 1
        runtime = seen_runtime[0]
        assert (runtime.max_steps, runtime.max_output_tokens, runtime.temperature) == (
            steps,
            2048,
            0.3,
        )
        assert runtime.request_extra == (
            {"enable_thinking": False} if provider == "qwen" else {"thinking": {"type": "disabled"}}
        )
    assert config.read_bytes() == before
    assert not (project / ".env").exists()
    logs = "\n".join(path.read_text() for path in (project / "logs").iterdir())
    assert "deepseek-test-key" not in logs and "qwen-test-key" not in logs


@pytest.mark.parametrize("missing", ["model", "key"])
def test_missing_model_settings_point_to_config(user_home, tmp_path, monkeypatch, capsys, missing):
    from cli import main as cli

    config = configure_user(SOURCE)
    if missing == "key":
        config.write_text(config.read_text().replace("LLM_MODEL=\n", "LLM_MODEL=test-model\n"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["repo-agent", "--sandbox", "local", "--sandbox-writeback", "manual"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code != 0
    output = capsys.readouterr().err
    assert str(config) in output
    assert ("LLM_MODEL" if missing == "model" else "DEEPSEEK_API_KEY") in output
