"""Installation and configuration contracts across independent workspaces."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli.config import CONFIG_KEYS, configured_environment, read_config, user_config_path
from cli.setup import configure_path, configure_user, install_command

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    home = tmp_path / "user home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in CONFIG_KEYS | {"AGENT_ENV_FILE", "AGENT_CONFIG_DIR", "XDG_CONFIG_HOME", "ZDOTDIR"}:
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
    assert values["LLM_PROVIDER"] == "deepseek"
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
def test_command_and_path_setup_preserves_existing_files(user_home, tmp_path, monkeypatch, shell):
    install = tmp_path / "agent's install"
    entry = install / ".venv/bin/repo-agent"
    entry.parent.mkdir(parents=True)
    entry.write_text("entry")
    bin_dir = user_home / ".local/bin"
    command = install_command(install, bin_dir)
    assert command.resolve() == entry
    assert install_command(install, bin_dir) == command
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
        install_command(install, bin_dir)
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
    args = [str(entry), "--sandbox", "local"]
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
    assert not (project / "run_agent.sh").exists()
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


@pytest.mark.parametrize("legacy_flag", [[], ["--non-interactive"]])
def test_setup_installs_working_command_without_modifying_projects(
    user_home, tmp_path, monkeypatch, legacy_flag
):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    project = tmp_path / "empty project"
    project.mkdir()
    setup = [
        sys.executable,
        "-m",
        "cli.setup",
        "--agent-home",
        str(SOURCE),
        "--skip-sandbox",
        *legacy_flag,
    ]
    for _ in range(2):
        result = subprocess.run(
            setup, cwd=project, stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15
        )
        assert result.returncode == 0, result.stderr
        assert str(user_config_path()) in result.stdout
        assert "首次启动前" in result.stdout
    assert list(project.iterdir()) == []
    config = user_config_path()
    assert read_config(config)["LLM_MODEL"] == ""
    config.write_text(
        config.read_text()
        .replace("LLM_MODEL=\n", "LLM_MODEL=global-model\n")
        .replace("DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY=fake-key-never-sent\n")
    )
    result = subprocess.run(
        [str(user_home / ".local/bin/repo-agent"), "--sandbox", "local"],
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
    from cli import setup

    monkeypatch.setattr(sys, "argv", ["setup", "--agent-home", str(SOURCE), "--non-interactive"])

    def failed_build(command, **kwargs):
        assert command == [sys.executable, "-m", "sandbox.build"]
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(setup.subprocess, "run", failed_build)
    with pytest.raises(SystemExit) as error:
        setup.main()
    assert error.value.code == 1
    assert user_config_path().exists()  # The template survives so the install can be retried.
    assert not (user_home / ".local/bin/repo-agent").exists()
    assert not (user_home / ".zshrc").exists()
