"""Git history against real repositories, with bounded and protected object reads."""

import json
from dataclasses import replace

import pytest
from test_git_diff import repository as repository

from host_support.cancellation import RunCancelled, cancellation_scope
from sandbox.worker import execute_request
from tools import GitLogTool, GitShowTool, ToolDispatcher
from tools._internal.process_runner import ProcessResult


@pytest.mark.parametrize("tool_type", [GitLogTool, GitShowTool])
def test_unknown_revision_is_reported(repository, tool_type):
    repo, _ = repository
    result = tool_type(repo).execute({"revision": "missing-branch"})
    assert result.error_code == "INVALID_REVISION"


def test_log_pagination_pins_commit_and_filters_literal_deleted_paths(repository):
    repo, git = repository
    first = git("rev-parse", "HEAD").stdout.decode().strip()
    (repo / "first").write_text("changed\n")
    git("commit", "-qam", "second 中文")
    tool = GitLogTool(repo)
    page = tool.execute({"limit": 1})
    assert page.success, page
    assert page.data["commits"][0]["subject"] == "second 中文"
    assert page.data["next_offset"] == 1 and page.data["truncated"]
    git("rm", "first")
    git("commit", "-qm", "third")
    next_page = tool.execute({"revision": page.data["resolved_revision"], "offset": 1, "limit": 1})
    assert next_page.success and next_page.data["commits"][0]["commit"] == first
    assert next_page.data["next_offset"] is None
    scoped = tool.execute({"paths": ["first"]})
    assert [c["subject"] for c in scoped.data["commits"]] == ["third", "second 中文", "fixture"]
    assert tool.execute({"paths": ["does-not-exist"]}).data["commits"] == []


def test_show_root_commit_patch_body_and_historical_deleted_file(repository):
    repo, git = repository
    tool = GitShowTool(repo)
    first = git("rev-parse", "HEAD").stdout.decode().strip()
    root = tool.execute({})
    assert root.success, root
    assert root.data["base_revision"] is None and "+first" in root.data["diff"]
    git("rm", "first")
    git("commit", "-qm", "remove\n\nfull message")
    result = tool.execute({"paths": ["first"], "context_lines": 0})
    assert result.success and result.data["base_revision"] == first
    assert "-first" in result.data["diff"] and "full message" in result.data["commit"]["message"]
    historical = tool.execute({"revision": first, "path": "first"})
    assert historical.success and historical.data["content"] == "first\n"
    assert tool.execute({"path": "first"}).error_code == "FILE_NOT_FOUND"
    assert not (repo / "first").exists()


def test_merge_shows_first_parent_diff(repository):
    repo, git = repository
    branch = git("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
    git("checkout", "-qb", "side")
    (repo / "side").write_text("side value\n")
    git("add", "side")
    git("commit", "-qm", "side")
    git("checkout", branch)
    (repo / "main").write_text("main value\n")
    git("add", "main")
    git("commit", "-qm", "main")
    parent = git("rev-parse", "HEAD").stdout.decode().strip()
    git("merge", "--no-ff", "side", "-m", "merge")
    result = GitShowTool(repo).execute({})
    assert result.success and result.data["base_revision"] == parent
    assert len(result.data["commit"]["parents"]) == 2
    assert "+side value" in result.data["diff"] and "+main value" not in result.data["diff"]


def test_protected_history_stays_excluded_after_delete_and_rename(repository):
    repo, git = repository
    (repo / ".env").write_text("UNIQUE_SECRET\n")
    (repo / "safe").write_text("public\n")
    git("add", "-f", ".env", "safe")
    git("commit", "-qm", "add files")
    protected_commit = git("rev-parse", "HEAD").stdout.decode().strip()
    tool = GitShowTool(repo)
    assert "UNIQUE_SECRET" not in tool.execute({}).data["diff"]
    git("rm", ".env")
    git("mv", "safe", "id_rsa")
    git("commit", "-qm", "delete and rename")
    result = tool.execute({})
    assert result.success and "UNIQUE_SECRET" not in result.data["diff"]
    assert "id_rsa" not in result.data["diff"]
    assert result.data["protected_paths_excluded"]
    assert (
        tool.execute({"path": ".env", "revision": protected_commit}).error_code == "PROTECTED_FILE"
    )
    assert GitLogTool(repo).execute({"paths": [".env"]}).error_code == "PROTECTED_FILE"


def test_paths_are_literal_and_cannot_escape_repository(repository):
    repo, git = repository
    for name in [":(glob)*", "-option", "space 中文.txt"]:
        (repo / name).write_text(name)
        git("--literal-pathspecs", "add", "--", name)
    git("commit", "-qm", "literal names")
    for name in [":(glob)*", "-option", "space 中文.txt"]:
        result = GitShowTool(repo).execute({"path": name})
        assert result.success and result.data["content"] == name
    assert GitShowTool(repo).execute({"path": "../outside"}).error_code == "PATH_OUTSIDE_WORKSPACE"
    (repo / "escape").symlink_to(repo.parent / "outside")
    assert GitShowTool(repo).execute({"path": "escape"}).error_code == "PATH_OUTSIDE_WORKSPACE"
    assert GitShowTool(repo).execute({"path": "alias"}).error_code == "NOT_A_FILE"


@pytest.mark.parametrize("kind", [GitLogTool, GitShowTool])
@pytest.mark.parametrize(
    "revision", ["--output=bad", "HEAD..main", "HEAD:first", "a b", "", None, True, "HEAD\n"]
)
def test_revision_injection_rejected_before_start(repository, kind, revision, monkeypatch):
    repo, _ = repository
    tool = kind(repo)
    monkeypatch.setattr(tool.runner, "run", lambda *a, **k: pytest.fail("process started"))
    assert tool.execute({"revision": revision}).error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "arguments",
    [
        {"limit": True},
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
        {"offset": 10001},
        {"offset": False},
        {"extra": 1},
    ],
)
def test_log_argument_limits(repository, arguments):
    repo, _ = repository
    assert GitLogTool(repo).execute(arguments).error_code == "INVALID_ARGUMENTS"


def test_local_external_filter_and_partial_clone_refused(repository):
    repo, git = repository
    git("config", "filter.test.clean", "touch SHOULD_NOT_RUN")
    for cls in (GitLogTool, GitShowTool):
        assert cls(repo).execute({}).error_code == "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX"
        assert cls(repo, execution_allowed=True).execute({}).success
    assert not (repo / "SHOULD_NOT_RUN").exists()
    git("config", "--unset", "filter.test.clean")
    git("config", "remote.origin.promisor", "true")
    for cls in (GitLogTool, GitShowTool):
        assert cls(repo).execute({}).error_code == "GIT_PARTIAL_CLONE_UNSUPPORTED"


def test_external_diff_textconv_signature_and_pager_never_execute(repository):
    repo, git = repository
    marker = repo.parent / "executed"
    script = repo.parent / "malicious.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    script.chmod(0o755)
    for key in (
        "diff.external",
        "diff.evil.command",
        "diff.evil.textconv",
        "core.pager",
        "gpg.program",
    ):
        git("config", key, str(script))
    git("config", "log.showSignature", "true")
    (repo / ".gitattributes").write_text("* diff=evil\n")
    (repo / "first").write_text("updated\n")
    git("add", "first", ".gitattributes")
    git("commit", "-qm", "update")
    assert GitShowTool(repo).execute({}).success
    assert GitLogTool(repo).execute({}).success
    assert not marker.exists()


def test_binary_oversize_and_commit_metadata_truncation(repository):
    repo, git = repository
    (repo / "binary").write_bytes(b"abc\0def")
    (repo / "large").write_text("x" * 5000)
    git("add", "binary", "large")
    git("commit", "-qm", "large")
    assert GitShowTool(repo).execute({"path": "binary"}).error_code == "BINARY_FILE"
    assert (
        GitShowTool(repo, max_output_bytes=1024).execute({"path": "large"}).error_code
        == "OUTPUT_TOO_LARGE"
    )
    patch = GitShowTool(repo, max_output_bytes=1024).execute({"paths": ["large"]})
    assert patch.success and patch.data["truncated"]
    assert GitLogTool(repo, max_output_bytes=100).execute({}).error_code == "OUTPUT_TOO_LARGE"


def test_cleanup_failure_and_cancellation_keep_process_semantics(repository, monkeypatch):
    repo, _ = repository
    tool = GitLogTool(repo)
    result = ProcessResult(
        0, "", "", False, "unknown cleanup", 1, False, False, cleanup_status="unknown"
    )
    monkeypatch.setattr(tool.runner, "run", lambda *a, **k: result)
    failed = tool.execute({})
    assert failed.error_code == "GIT_CLEANUP_FAILED" and failed.data["cleanup_status"] == "unknown"
    assert failed.effects.details["cleanup_error"] == "unknown cleanup"
    monkeypatch.setattr(
        tool.runner,
        "run",
        lambda *a, **k: replace(
            result, timed_out=True, cleanup_error=None, cleanup_status="confirmed"
        ),
    )
    assert tool.execute({}).error_code == "GIT_TIMEOUT"
    with cancellation_scope() as context:
        context.cancel()
        with pytest.raises(RunCancelled):
            tool.execute({})


def test_dispatch_and_worker_registration(repository, capsys):
    repo, _ = repository
    dispatcher = ToolDispatcher()
    for cls in (GitLogTool, GitShowTool):
        tool = cls(repo)
        dispatcher.register(tool)
        assert dispatcher.execute(tool.definition.name, {}).success
    execute_request({"name": "git_log", "arguments": {"limit": 1}}, repo)
    assert json.loads(capsys.readouterr().out)["success"]
