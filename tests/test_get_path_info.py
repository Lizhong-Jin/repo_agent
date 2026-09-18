import os
from pathlib import Path

import pytest

from tools import GetPathInfoTool


def test_regular_file_metadata(tmp_path):
    target = tmp_path / "source.txt"
    target.write_bytes("中文\n".encode())
    result = GetPathInfoTool(tmp_path).execute({"path": target.name})
    assert result.success
    assert result.data == {
        "path": "source.txt",
        "type": "file",
        "size": 7,
        "executable": os.access(target, os.X_OK),
    }


@pytest.mark.parametrize("path", [".", "nested"])
def test_directory_metadata(tmp_path, path):
    (tmp_path / "nested").mkdir()
    result = GetPathInfoTool(tmp_path).execute({"path": path})
    assert result.success
    assert result.data == {"path": path, "type": "directory"}


@pytest.mark.parametrize("destination_type", ["file", "directory", "missing"])
def test_link_metadata_keeps_link_name(tmp_path, destination_type):
    target = tmp_path / "target"
    if destination_type == "file":
        target.write_text("ordinary")
    elif destination_type == "directory":
        target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=destination_type == "directory")
    result = GetPathInfoTool(tmp_path).execute({"path": "alias"})
    assert result.success
    assert result.data == {"path": "alias", "type": "symlink"}


def test_link_under_parent_alias_preserves_final_name(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "file").write_text("ordinary")
    (real / "link").symlink_to(real / "file")
    (tmp_path / "parent_alias").symlink_to(real, target_is_directory=True)
    result = GetPathInfoTool(tmp_path).execute({"path": "parent_alias/link"})
    assert result.success
    assert result.data == {"path": "real/link", "type": "symlink"}


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.local/child"])
def test_credential_path_is_rejected(tmp_path, name):
    result = GetPathInfoTool(tmp_path).execute({"path": name})
    assert result.error_code == "PROTECTED_FILE"


@pytest.mark.parametrize("reverse", [False, True])
def test_credential_symlink_cannot_hide_either_spelling(tmp_path, reverse):
    target = tmp_path / ("ordinary" if reverse else ".env")
    alias = tmp_path / (".env" if reverse else "alias")
    target.write_text("private content")
    alias.symlink_to(target)
    result = GetPathInfoTool(tmp_path).execute({"path": alias.name})
    assert result.error_code == "PROTECTED_FILE"
    assert "private content" not in str(result)


def test_custom_credential_target_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "config.txt"
    target.write_text("private content")
    monkeypatch.setenv("AGENT_ENV_FILE", str(target))
    (tmp_path / "alias").symlink_to(target)
    result = GetPathInfoTool(tmp_path).execute({"path": "alias"})
    assert result.error_code == "PROTECTED_FILE"


@pytest.mark.parametrize("use_link", [False, True])
def test_outside_target_is_rejected(tmp_path, use_link):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("ordinary")
    (workspace / "alias").symlink_to(outside)
    result = GetPathInfoTool(workspace).execute({"path": "alias" if use_link else "../outside"})
    assert result.error_code == "PATH_OUTSIDE_WORKSPACE"


def test_outside_link_pointing_into_workspace_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "file"
    target.write_text("ordinary")
    alias = tmp_path / "outside_alias"
    alias.symlink_to(target)
    result = GetPathInfoTool(workspace).execute({"path": str(alias)})
    assert result.error_code == "PATH_OUTSIDE_WORKSPACE"


def test_loop_is_an_explicit_stat_error(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    result = GetPathInfoTool(tmp_path).execute({"path": "loop"})
    assert result.error_code == "STAT_ERROR"


def test_missing_path_and_non_directory_parent_are_distinct(tmp_path):
    (tmp_path / "file").write_text("ordinary")
    tool = GetPathInfoTool(tmp_path)
    assert tool.execute({"path": "missing"}).error_code == "FILE_NOT_FOUND"
    assert tool.execute({"path": "file/child"}).error_code == "NOT_A_DIRECTORY"


def test_permission_failure_returns_tool_error(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("ordinary")
    tool = GetPathInfoTool(tmp_path)

    def deny_stat(*args, **kwargs):
        raise PermissionError("simulated permission failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", deny_stat)
        result = tool.execute({"path": "file"})
    assert result.error_code == "PERMISSION_DENIED"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Requires named pipes")
def test_special_file_is_inspected_without_opening_it(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    result = GetPathInfoTool(tmp_path).execute({"path": "pipe"})
    assert result.success
    assert result.data == {"path": "pipe", "type": "other"}


@pytest.mark.parametrize(
    "arguments",
    [None, {}, {"path": ""}, {"path": "\x00"}, {"path": 1}, {"path": ".", "extra": True}],
)
def test_invalid_arguments(tmp_path, arguments):
    assert GetPathInfoTool(tmp_path).execute(arguments).error_code == "INVALID_ARGUMENTS"
