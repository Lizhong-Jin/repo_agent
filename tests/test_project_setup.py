import subprocess
from pathlib import Path

import pytest

from cli.init_project import initialize_project


def test_initialize_preserves_config_and_is_repeatable(tmp_path):
    install = tmp_path / "agent's install"
    project = tmp_path / "task"
    install.mkdir()
    project.mkdir()
    (install / ".env.example").write_text("LLM_MODEL=example\n")
    (install / ".env").write_text("private-install-key")
    (project / ".gitignore").write_text("existing-rule")
    initialize_project(project, install)
    assert (project / ".env").read_text() == "LLM_MODEL=example\n"
    assert (project / ".env").stat().st_mode & 0o777 == 0o600
    assert (project / "run_agent.sh").stat().st_mode & 0o100
    subprocess.run(["bash", "-n", str(project / "run_agent.sh")], check=True)
    (project / ".env").write_text("DEEPSEEK_API_KEY=my-project-key\nLLM_MODEL=my-model\n")
    (install / ".env.example").write_text("LLM_MODEL=example\nLLM_THINKING=auto\n")
    before = (project / ".gitignore").read_text()
    initialize_project(project, install)
    updated = (project / ".env").read_text()
    assert "DEEPSEEK_API_KEY=my-project-key\nLLM_MODEL=my-model\n" in updated
    assert "LLM_THINKING=auto\n" in updated
    initialize_project(project, install)
    assert (project / ".env").read_text() == updated
    assert (project / ".gitignore").read_text() == before
    assert "existing-rule\n" in before and "/logs/\n" in before
    assert not (project / ".venv").exists()


def test_init_entry_without_model_or_key(tmp_path):
    source = Path(__file__).resolve().parents[1] / "run_agent.sh"
    result = subprocess.run(
        [str(source), "--init"], cwd=tmp_path, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "run_agent.sh").exists()
    assert (tmp_path / ".env").exists()
    assert not (tmp_path / "logs").exists()


def test_personal_defaults_seed_projects_without_overwriting_existing_settings(tmp_path):
    install = tmp_path / "install"
    project = tmp_path / "task"
    install.mkdir()
    project.mkdir()
    (install / ".env.example").write_text(
        "LLM_MODEL=example\nAGENT_MAX_STEPS=8\nLLM_THINKING=auto\nDEEPSEEK_API_KEY=\n"
    )
    (install / ".env.defaults").write_text(
        'export LLM_MODEL="personal-model"\nAGENT_MAX_STEPS=12\nDEEPSEEK_API_KEY=test-key\n'
    )
    initialize_project(project, install)
    config = (project / ".env").read_text()
    assert 'LLM_MODEL="personal-model"' in config
    assert "AGENT_MAX_STEPS=12" in config
    assert "LLM_THINKING=auto" in config  # New schema fields still get a default.
    assert "DEEPSEEK_API_KEY=test-key" in config
    assert (project / ".env").stat().st_mode & 0o777 == 0o600

    (install / ".env.defaults").write_text("LLM_MODEL=new-model\nDEEPSEEK_API_KEY=new-key\n")
    initialize_project(project, install)
    assert (project / ".env").read_text() == config
    # Explicit empty project values must not unexpectedly inherit credentials on a later init.
    (project / ".env").write_text("LLM_MODEL=project-model\nDEEPSEEK_API_KEY=\n")
    initialize_project(project, install)
    updated = (project / ".env").read_text()
    assert "LLM_MODEL=project-model" in updated
    assert "DEEPSEEK_API_KEY=\n" in updated
    assert "new-key" not in updated


@pytest.mark.parametrize(
    "content",
    [
        "DEEPSEEK_API_KEY='secret",
        "UNKNOWN_OPTION=secret",
        "LLM_MODEL=one\nLLM_MODEL=secret",
    ],
)
def test_invalid_defaults_fail_before_creating_project_files(tmp_path, content):
    install = tmp_path / "install"
    project = tmp_path / "task"
    install.mkdir()
    project.mkdir()
    (install / ".env.example").write_text("LLM_MODEL=example\nDEEPSEEK_API_KEY=\n")
    (install / ".env.defaults").write_text(content)
    with pytest.raises(ValueError) as error:
        initialize_project(project, install)
    assert "secret" not in str(error.value)
    assert list(project.iterdir()) == []
