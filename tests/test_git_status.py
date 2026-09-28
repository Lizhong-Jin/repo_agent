import os
import shutil
import subprocess

import pytest

from tools._internal.process_runner import ProcessResult, ProcessStartError
from tools.git_tools import GitStatusTool


@pytest.fixture
def repository(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("Git is required for repository integration tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = GitStatusTool._git_environment()
    env.update(
        HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    monkeypatch.delenv("AGENT_ENV_FILE", raising=False)
    monkeypatch.setattr(GitStatusTool, "_git_environment", staticmethod(lambda: dict(env)))

    def git(*args, check=True):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=repo,
            env=env,
            check=check,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("baseline\n")
    (repo / "old name.txt").write_text("rename fixture\n")
    git("add", "--all")
    git("commit", "-qm", "fixture")
    return repo, git


def test_workspace_relative_paths_and_rename_with_special_characters(repository):
    repo, git = repository
    new_name = "new\n中文.txt"
    git("mv", "old name.txt", new_name)
    (repo / "tracked.txt").write_text("staged\n")
    git("add", "tracked.txt")
    (repo / "tracked.txt").write_text("unstaged\n")
    (repo / "subdir").mkdir()

    result = GitStatusTool(repo.parent).execute({"cwd": "repo/subdir", "paths": ["repo"]})

    assert result.success
    assert result.data["repo_root"] == "repo"
    assert result.data["scope"]["paths"] == ["repo"]
    entries = {entry["path"]: entry for entry in result.data["entries"]}
    assert entries[f"repo/{new_name}"]["original_path"] == "repo/old name.txt"
    assert entries[f"repo/{new_name}"]["staged_status"] == "renamed"
    assert entries["repo/tracked.txt"]["staged_status"] == "modified"
    assert entries["repo/tracked.txt"]["worktree_status"] == "modified"


def test_scope_clean_does_not_claim_repository_is_clean(repository):
    repo, _ = repository
    (repo / "tracked.txt").write_text("changed\n")
    tool = GitStatusTool(repo)
    assert tool.execute({}).data["scope_clean"] is False
    for path in ("old name.txt", "missing.txt"):
        result = tool.execute({"paths": [path], "include_untracked": False})
        assert result.success
        assert "clean" not in result.data
        assert result.data["scope_clean"] is True
        assert result.data["scope"] == {
            "paths": [path],
            "include_untracked": False,
            "protected_paths_excluded": True,
        }


def test_untracked_files_are_individual_and_can_be_excluded(repository):
    repo, _ = repository
    directory = repo / "newdir"
    directory.mkdir()
    for name in ("one.txt", "two.txt", ".hidden"):
        (directory / name).write_text("new\n")
    tool = GitStatusTool(repo)
    result = tool.execute({})
    assert result.success
    assert {entry["path"] for entry in result.data["entries"]} == {
        "newdir/one.txt",
        "newdir/two.txt",
        "newdir/.hidden",
    }
    assert all(entry["untracked"] for entry in result.data["entries"])
    assert result.data["total_entries"] == 3
    excluded = tool.execute({"include_untracked": False})
    assert excluded.data["scope_clean"] is True
    assert excluded.data["scope"]["include_untracked"] is False


def test_status_does_not_refresh_index(repository):
    repo, git = repository
    index = repo / ".git" / "index"
    before = index.read_bytes()
    tracked = repo / "tracked.txt"
    stat = tracked.stat()
    os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 3_000_000_000))

    result = GitStatusTool(repo).execute({})
    assert result.success
    assert result.data["scope_clean"] is True
    assert index.read_bytes() == before
    # Verify the fixture actually triggers the optional refresh in ordinary status.
    git("status", "--porcelain=v2", "-z")
    assert index.read_bytes() != before


def test_credentials_filtered_before_counts_and_entry_limit(repository, monkeypatch):
    repo, git = repository
    (repo / ".env").write_text("fake fixture\n")
    git("add", ".env")
    (repo / ".env").write_text("changed fake fixture\n")
    (repo / ".env.local").mkdir()
    (repo / ".env.local" / "config").write_text("fake fixture\n")
    (repo / "custom.cfg").write_text("fake fixture\n")
    monkeypatch.setenv("AGENT_ENV_FILE", str(repo / "custom.cfg"))
    (repo / "alias").symlink_to("custom.cfg")
    for name in ("visible-a", "visible-b"):
        (repo / name).write_text("ordinary\n")

    result = GitStatusTool(repo, max_entries=1).execute({})
    assert result.success
    assert [entry["path"] for entry in result.data["entries"]] == ["visible-a"]
    assert result.data["total_entries"] == 2
    assert result.data["returned_entries"] == 1
    assert result.data["truncated"] is True
    assert result.data["scope_clean"] is False
    for path in (".env", ".env.local", "custom.cfg", "alias"):
        assert GitStatusTool(repo).execute({"paths": [path]}).error_code == "PROTECTED_FILE"


@pytest.mark.parametrize(
    "old,new",
    [
        (".env", "public.txt"),
        ("public.txt", ".env"),
        ("custom.cfg", "public.txt"),
        ("public.txt", "custom.cfg"),
    ],
)
def test_rename_hides_both_sides_when_either_is_protected(repository, monkeypatch, old, new):
    repo, git = repository
    monkeypatch.setenv("AGENT_ENV_FILE", str(repo / "custom.cfg"))
    (repo / old).write_text("unique fake credential fixture\n")
    git("add", old)
    git("commit", "-qm", "rename fixture")
    git("mv", old, new)
    result = GitStatusTool(repo).execute({})
    assert result.success
    assert result.data["entries"] == []
    assert result.data["total_entries"] == 0
    assert result.data["scope_clean"] is True
    assert result.data["scope"]["protected_paths_excluded"] is True


def test_selected_symlink_reports_entry_not_target(repository):
    repo, git = repository
    link = repo / "alias"
    link.symlink_to("tracked.txt")
    git("add", "alias")
    git("commit", "-qm", "link fixture")
    link.unlink()
    link.symlink_to("missing")
    result = GitStatusTool(repo).execute({"paths": ["alias"]})
    assert result.success
    assert result.data["entries"][0]["path"] == "alias"
    assert result.data["entries"][0]["worktree_status"] == "modified"


def test_actual_output_overflow_returns_structured_error(repository):
    repo, _ = repository
    for number in range(20):
        (repo / (str(number) + "x" * 80)).write_text("new\n")
    result = GitStatusTool(repo, max_output_bytes=512).execute({})
    assert result.error_code == "GIT_STATUS_OUTPUT_TOO_LARGE"


def test_conflicted_file(repository):
    repo, git = repository
    git("checkout", "-qb", "other")
    (repo / "tracked.txt").write_text("other branch\n")
    git("commit", "-qam", "other")
    git("checkout", "-q", "main")
    (repo / "tracked.txt").write_text("main branch\n")
    git("commit", "-qam", "main")
    assert git("merge", "other", check=False).returncode == 1
    result = GitStatusTool(repo).execute({})
    assert result.success
    entry = result.data["entries"][0]
    assert entry["path"] == "tracked.txt"
    assert entry["conflicted"] is True
    assert entry["staged_status"] == "unmerged"
    assert entry["worktree_status"] == "unmerged"


def test_detached_head(repository):
    repo, git = repository
    git("checkout", "--detach", "-q")
    result = GitStatusTool(repo).execute({})
    assert result.success
    assert result.data["detached"] is True
    assert result.data["branch"] is None
    assert result.data["head"] == git("rev-parse", "HEAD").stdout.decode().strip()


def process_result(**kwargs):
    defaults = dict(
        exit_code=0,
        stdout="",
        stderr="",
        timed_out=False,
        cleanup_error=None,
        duration_ms=1,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    return ProcessResult(**(defaults | kwargs))


@pytest.mark.parametrize("stage", ["detection", "status"])
@pytest.mark.parametrize(
    "cause,expected_code",
    [
        (FileNotFoundError(), "GIT_NOT_FOUND"),
        (PermissionError(), "PERMISSION_DENIED"),
        (OSError(), "GIT_ERROR"),
        (RuntimeError(), "GIT_ERROR"),
    ],
)
def test_process_start_failures(tmp_path, monkeypatch, stage, cause, expected_code):
    tool = GitStatusTool(tmp_path)

    def run(command, **kwargs):
        if stage == "status" and "rev-parse" in command:
            return process_result(stdout=str(tmp_path.resolve()) + "\n")
        if stage == "status" and "config" in command:
            return process_result(exit_code=1)
        raise ProcessStartError(cause)

    monkeypatch.setattr(tool.runner, "run", run)
    assert tool.execute({}).error_code == expected_code


@pytest.mark.parametrize("stage", ["detection", "status"])
@pytest.mark.parametrize("failure", ["timeout", "truncation"])
def test_runner_timeout_and_truncation(tmp_path, monkeypatch, stage, failure):
    tool = GitStatusTool(tmp_path)

    def run(command, **kwargs):
        if stage == "status" and "rev-parse" in command:
            return process_result(stdout=str(tmp_path.resolve()) + "\n")
        if stage == "status" and "config" in command:
            return process_result(exit_code=1)
        if failure == "timeout":
            return process_result(exit_code=None, timed_out=True)
        return process_result(stdout="incomplete", stdout_truncated=True)

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute({})
    assert result.error_code == (
        "GIT_TIMEOUT" if failure == "timeout" else "GIT_STATUS_OUTPUT_TOO_LARGE"
    )
    if stage == "status" and failure == "timeout":
        assert result.error == "git status timed out."
