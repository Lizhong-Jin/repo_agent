"""Regression tests use only synthetic secrets and isolated temporary workspaces."""

import os
import subprocess

import pytest

from sandbox.policy import SandboxPolicy
from sandbox.session import files
from tools.execute import RunCommandTool, RunPythonTool
from tools.factory import create_default_tools
from tools.filesystem import ListFileTool, ReadFileTool, SearchFilesTool, WriteFileTool
from tools.git_tools import GitDiffTool


@pytest.mark.parametrize(
    "name",
    [
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".kube/config",
        ".docker/config.json",
        ".git/config",
        ".agents/policy",
        ".codex/config.toml",
        "logs/session.log",
        "id_ed25519",
        "private.pem",
        "private.key",
        "bundle.p12",
        ".env.private",
        ".repo-agent-install.json",
    ],
)
def test_protected_files_unavailable_to_local_tools_and_snapshot(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SYNTHETIC_SECRET")
    assert (
        ReadFileTool(tmp_path)
        .execute({"reads": [{"path": name}]})
        .data["results"][0]["error"]["code"]
        == "PROTECTED_FILE"
    )
    assert (
        WriteFileTool(tmp_path)
        .execute(
            {
                "path": name,
                "content": "replacement",
                "overwrite": True,
            }
        )
        .error_code
        == "PROTECTED_FILE"
    )
    assert name not in files(tmp_path, SandboxPolicy())
    result = SearchFilesTool(tmp_path).execute(
        {"query": "SYNTHETIC_SECRET", "include_hidden": True}
    )
    assert result.success
    assert "SYNTHETIC_SECRET" not in str(result.data.get("matches"))
    assert path.read_text() == "SYNTHETIC_SECRET"


def test_aliases_and_custom_directories_are_protected(tmp_path, monkeypatch):
    secret = tmp_path / ".env"
    secret.write_text("SYNTHETIC_SECRET")
    (tmp_path / "symlink.txt").symlink_to(secret)
    os.link(secret, tmp_path / "hardlink.txt")
    for name in ("symlink.txt", "hardlink.txt"):
        assert (
            ReadFileTool(tmp_path)
            .execute({"reads": [{"path": name}]})
            .data["results"][0]["error"]["code"]
            == "PROTECTED_FILE"
        )
    custom = tmp_path / "audit"
    monkeypatch.setenv("AGENT_LOG_DIR", str(custom))
    # A configured directory is protected even before it exists.
    assert (
        WriteFileTool(tmp_path)
        .execute(
            {
                "path": "audit/new.log",
                "content": "x",
                "create_parents": True,
            }
        )
        .error_code
        == "PROTECTED_FILE"
    )
    custom.mkdir()
    (custom / "session.txt").write_text("SYNTHETIC_SECRET")
    assert ListFileTool(tmp_path).execute({"path": "audit"}).error_code == "PROTECTED_FILE"
    result = ListFileTool(tmp_path).execute({"path": ".", "include_hidden": True})
    assert result.success
    assert "audit" not in str(result.data)
    assert "hardlink.txt" not in str(result.data)


def test_commands_default_to_denial_without_spawning(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No host subprocess should start")

    for tool, arguments in [
        (RunCommandTool(tmp_path), {"command": ["sh", "-c", "cat .env"]}),
        (RunPythonTool(tmp_path), {"code": "open('.env').read()"}),
    ]:
        monkeypatch.setattr(tool.runner, "run", forbidden)
        assert tool.execute(arguments).error_code == "SANDBOX_REQUIRED"
    names = {t.definition.name for t in create_default_tools(tmp_path)}
    assert not {"run_command", "run_python"} & names
    isolated = create_default_tools(tmp_path, isolated_execution=True)
    assert {"run_command", "run_python"} <= {t.definition.name for t in isolated}


@pytest.mark.parametrize("paths", [[], ["."], ["nested"]])
def test_git_diff_filters_protected_content_for_directory_scopes(tmp_path, paths):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/.env").write_text("SYNTHETIC_SECRET")
    (tmp_path / "nested/ok.txt").write_text("ordinary text")
    git("add", "-f", ".")
    result = GitDiffTool(tmp_path).execute({"mode": "staged", "paths": paths})
    assert result.success
    assert "SYNTHETIC_SECRET" not in result.data["diff"]
    assert ".env" not in result.data["diff"]
    assert "ordinary text" in result.data["diff"]


def test_ordinary_file_edit_still_works(tmp_path):
    result = WriteFileTool(tmp_path).execute({"path": "main.py", "content": "print(1)\n"})
    assert result.success
    assert ReadFileTool(tmp_path).execute({"reads": [{"path": "main.py"}]}).success
