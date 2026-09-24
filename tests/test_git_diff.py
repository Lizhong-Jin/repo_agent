import os
import shutil
import subprocess

import pytest

from tools._internal.process_runner import ProcessResult, ProcessStartError
from tools.git_tools import GitDiffTool


@pytest.fixture
def repository(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("Git is required for repository integration tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = GitDiffTool._git_environment()
    env.update(
        HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    monkeypatch.setattr(GitDiffTool, "_git_environment", staticmethod(lambda: dict(env)))

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q")
    hooks = tmp_path / "empty_hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    (repo / "first").write_text("first\n")
    (repo / "second").write_text("second\n")
    (repo / "directory").mkdir()
    (repo / "directory" / "file").write_text("nested\n")
    (repo / "alias").symlink_to("first")
    git("add", "--all")
    git("commit", "-qm", "fixture")
    return repo, git


@pytest.mark.parametrize("mode", ["working", "staged", "head"])
@pytest.mark.parametrize("target", ["second", "directory", "missing"])
def test_diff_selects_modified_link_itself(repository, mode, target):
    repo, git = repository
    (repo / "alias").unlink()
    (repo / "alias").symlink_to(target, target_is_directory=target == "directory")
    if mode != "working":
        git("add", "--", "alias")

    result = GitDiffTool(repo).execute({"paths": ["alias"], "mode": mode})

    assert result.success
    assert result.data["has_changes"]
    assert "diff --git a/alias b/alias" in result.data["diff"]
    assert "-first" in result.data["diff"]
    assert f"+{target}" in result.data["diff"]
    assert "diff --git a/second" not in result.data["diff"]


def test_unchanged_link_does_not_select_changed_target(repository):
    repo, _ = repository
    (repo / "first").write_text("target changed\n")
    result = GitDiffTool(repo).execute({"paths": ["alias"]})
    assert result.success
    assert result.data["has_changes"] is False
    assert result.data["diff"] == ""


@pytest.mark.parametrize("mode", ["working", "staged", "head"])
def test_deleted_link_is_still_selected(repository, mode):
    repo, git = repository
    (repo / "alias").unlink()
    if mode != "working":
        git("add", "--", "alias")
    result = GitDiffTool(repo).execute({"paths": ["alias"], "mode": mode})
    assert result.success
    assert "diff --git a/alias b/alias" in result.data["diff"]
    assert "deleted file mode 120000" in result.data["diff"]


def test_parent_alias_is_resolved_without_following_final_link(repository):
    repo, git = repository
    link = repo / "directory" / "nested_link"
    link.symlink_to("../first")
    git("add", "--", "directory/nested_link")
    git("commit", "-qm", "nested link fixture")
    link.unlink()
    link.symlink_to("../second")
    (repo / "parent_alias").symlink_to("directory", target_is_directory=True)
    result = GitDiffTool(repo).execute({"paths": ["parent_alias/nested_link"]})
    assert result.success
    assert "diff --git a/directory/nested_link b/directory/nested_link" in result.data["diff"]


def test_workspace_relative_paths_with_subdirectory_cwd(repository):
    repo, _ = repository
    (repo / "alias").unlink()
    (repo / "alias").symlink_to("second")
    result = GitDiffTool(repo.parent).execute({"cwd": "repo/directory", "paths": ["repo/alias"]})
    assert result.success
    assert result.data["repo_root"] == "repo"
    assert "diff --git a/alias b/alias" in result.data["diff"]


@pytest.mark.parametrize("target", [".env", ".env.local/file", "config.txt"])
def test_link_to_credentials_is_rejected(repository, monkeypatch, target):
    repo, _ = repository
    if target == "config.txt":
        monkeypatch.setenv("AGENT_ENV_FILE", str(repo / target))
    (repo / "alias").unlink()
    (repo / "alias").symlink_to(target)
    result = GitDiffTool(repo).execute({"paths": ["alias"]})
    assert result.error_code == "PROTECTED_FILE"


@pytest.mark.parametrize("workspace_is_repo", [False, True])
def test_link_target_outside_repository_is_rejected(repository, workspace_is_repo):
    repo, _ = repository
    (repo.parent / "outside").write_text("ordinary")
    (repo / "alias").unlink()
    (repo / "alias").symlink_to("../outside")
    tool = GitDiffTool(repo if workspace_is_repo else repo.parent)
    args = {"paths": ["alias"]} if workspace_is_repo else {"cwd": "repo", "paths": ["repo/alias"]}
    result = tool.execute(args)
    assert result.error_code == (
        "PATH_OUTSIDE_WORKSPACE" if workspace_is_repo else "PATH_OUTSIDE_REPOSITORY"
    )


def test_link_entry_outside_repository_cannot_select_target_inside(repository):
    repo, _ = repository
    (repo.parent / "outside_alias").symlink_to(repo / "first")
    result = GitDiffTool(repo.parent).execute({"cwd": "repo", "paths": ["outside_alias"]})
    assert result.error_code == "PATH_OUTSIDE_REPOSITORY"


def process_result(*, stdout="", stderr="", exit_code=0, timed_out=False):
    return ProcessResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        cleanup_error=None,
        duration_ms=1,
        stdout_truncated=False,
        stderr_truncated=False,
    )


@pytest.mark.parametrize("stage", ["detection", "diff"])
@pytest.mark.parametrize(
    ("cause", "expected_code"),
    [
        (FileNotFoundError(), "GIT_NOT_FOUND"),
        (PermissionError(), "PERMISSION_DENIED"),
        (OSError(), "GIT_ERROR"),
        (RuntimeError(), "GIT_ERROR"),
    ],
)
def test_process_start_failure_mapping(tmp_path, monkeypatch, stage, cause, expected_code):
    tool = GitDiffTool(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if stage == "diff" and len(calls) == 1:
            return process_result(stdout=str(tmp_path.resolve()) + "\n")
        if stage == "diff" and "config" in command:
            return process_result(exit_code=1)
        raise ProcessStartError(cause)

    monkeypatch.setattr(tool.runner, "run", run)
    assert tool.execute({}).error_code == expected_code
    assert len(calls) == (1 if stage == "detection" else 3)


@pytest.mark.parametrize("stage", ["detection", "diff"])
def test_timeout_result_is_handled_before_exit_code(tmp_path, monkeypatch, stage):
    tool = GitDiffTool(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if stage == "diff" and len(calls) == 1:
            return process_result(stdout=str(tmp_path.resolve()) + "\n")
        if stage == "diff" and "config" in command:
            return process_result(exit_code=1)
        return process_result(exit_code=None, timed_out=True)

    monkeypatch.setattr(tool.runner, "run", run)
    assert tool.execute({}).error_code == "GIT_TIMEOUT"
    assert len(calls) == (1 if stage == "detection" else 3)


def test_non_repository_is_distinct_from_timeout(tmp_path, monkeypatch):
    tool = GitDiffTool(tmp_path)
    monkeypatch.setattr(
        tool.runner,
        "run",
        lambda *args, **kwargs: process_result(exit_code=128, stderr="fatal: not a git repository"),
    )
    assert tool.execute({}).error_code == "NOT_A_GIT_REPOSITORY"
