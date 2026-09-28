"""Git filter commands must not execute through local read-only tools."""

import os
import shlex
import subprocess
import sys
from dataclasses import replace

import pytest
import test_git_diff as git_fixtures

from tools.factory import create_default_tools
from tools.git_tools import GitDiffTool, GitStatusTool

repository = git_fixtures.repository
process_result = git_fixtures.process_result


CALLS = [(GitDiffTool, {"mode": mode}) for mode in ("working", "head", "staged")] + [
    (GitStatusTool, {})
]


def filter_command(marker):
    # Synthetic marker only; a process filter need not complete its handshake to
    # demonstrate that its executable was started (and must instead be blocked).
    return shlex.join(
        [
            sys.executable,
            "-c",
            f"import sys; from pathlib import Path; Path({str(marker)!r}).write_text('ran'); "
            "sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ]
    )


@pytest.mark.parametrize("tool_type,arguments", CALLS)
@pytest.mark.parametrize("driver", ["clean", "process"])
@pytest.mark.parametrize("source", ["local", "include", "conditional", "worktree"])
def test_local_rejects_external_filters_before_any_diff_or_status(
    repository,
    tmp_path,
    monkeypatch,
    tool_type,
    arguments,
    driver,
    source,
):
    repo, git = repository
    marker = tmp_path / "outside-workspace-marker"
    (repo / ".gitattributes").write_text("first filter=probe\n")
    (repo / "first").write_text("FIRST\n")  # Same size also exercises status conversion.
    git("add", ".gitattributes", "first")
    (repo / "first").write_text("other\n")
    key = f"filter.probe.{driver}"
    command = filter_command(marker)
    if source == "local":
        git("config", key, command)
    elif source == "worktree":
        git("config", "extensions.worktreeConfig", "true")
        git("config", "--worktree", key, command)
    else:
        config = repo / "included-config"
        git("config", "--file", str(config), key, command)
        if source == "include":
            git("config", "include.path", str(config))
        else:
            branch = git("symbolic-ref", "--short", "HEAD").stdout.decode().strip()
            git("config", f"includeIf.onbranch:{branch}.path", str(config))
    tool = tool_type(repo)
    original = tool.runner.run
    invoked = []

    def run(command, **kwargs):
        invoked.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute(arguments)
    assert result.error_code == "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX", result
    assert not marker.exists()
    assert not any("diff" in command or "status" in command for command in invoked)
    assert str(marker) not in str(result)  # Do not echo configured commands.


@pytest.mark.parametrize("tool_type,arguments", CALLS)
def test_unused_filter_is_also_refused(repository, tmp_path, tool_type, arguments):
    repo, git = repository
    git("config", "filter.unused.clean", filter_command(tmp_path / "marker"))
    result = tool_type(repo).execute(arguments)
    assert result.error_code == "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX"


@pytest.mark.parametrize("tool_type", [GitDiffTool, GitStatusTool])
@pytest.mark.parametrize("driver", ["clean", "process"])
@pytest.mark.parametrize("command", ["", " ", "\u00a0"])
def test_only_truly_empty_filter_commands_are_allowed(repository, tool_type, driver, command):
    repo, git = repository
    git("config", f"filter.probe.{driver}", command)
    result = tool_type(repo).execute({})
    if command:
        assert result.error_code == "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX"
    else:
        assert result.success, result


@pytest.mark.parametrize("tool_type", [GitDiffTool, GitStatusTool])
def test_configuration_check_fails_closed(tmp_path, monkeypatch, tool_type):
    tool = tool_type(tmp_path)
    failures = [
        process_result(exit_code=128, stderr="private-config-details"),
        process_result(exit_code=1, stdout="unexpected"),
        process_result(stdout="malformed"),
        replace(process_result(stdout="filter.x.clean\ncommand\0"), stdout_truncated=True),
        replace(process_result(exit_code=1), stderr_truncated=True),
        replace(process_result(exit_code=1), cleanup_error="unknown"),
    ]
    for failure in failures:
        monkeypatch.setattr(tool.runner, "run", lambda *a, failure=failure, **kw: failure)
        result = tool._check_external_filters(tmp_path)
        assert result.error_code == "GIT_CONFIG_CHECK_FAILED"
        assert "private-config-details" not in str(result)


def test_filter_added_between_diff_phases_is_refused(repository, tmp_path, monkeypatch):
    repo, git = repository
    (repo / "first").write_text("changed\n")
    marker = tmp_path / "marker"
    tool = GitDiffTool(repo)
    original = tool.runner.run

    def run(command, **kwargs):
        result = original(command, **kwargs)
        if "--name-only" in command:
            git("config", "filter.new.clean", filter_command(marker))
        return result

    monkeypatch.setattr(tool.runner, "run", run)
    assert tool.execute({}).error_code == "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX"
    assert not marker.exists()


@pytest.mark.parametrize("isolated", [False, True])
def test_only_trusted_factory_setting_allows_filters(tmp_path, isolated):
    tools = create_default_tools(tmp_path, isolated_execution=isolated)
    git_tools = [tool for tool in tools if isinstance(tool, (GitDiffTool, GitStatusTool))]
    assert len(git_tools) == 2
    for tool in git_tools:
        assert tool.execution_allowed is isolated
        assert tool.execute({"execution_allowed": True}).error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("tool_type", [GitDiffTool, GitStatusTool])
def test_submodule_worktree_filters_are_not_entered(repository, tmp_path, tool_type):
    repo, git = repository
    source = tmp_path / "source"
    source.mkdir()
    env = GitDiffTool._git_environment()

    def child_git(*args):
        return subprocess.run(["git", *args], cwd=source, env=env, check=True, capture_output=True)

    child_git("init", "-q")
    (source / "file").write_text("before\n")
    (source / ".gitattributes").write_text("file filter=probe\n")
    child_git("add", ".")
    child_git("commit", "-qm", "initial")
    git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "sub")
    git("commit", "-qm", "submodule")
    marker = tmp_path / "submodule-marker"
    git("-C", "sub", "config", "filter.probe.clean", filter_command(marker))
    git("config", "diff.submodule", "diff")
    git("config", "submodule.sub.ignore", "none")
    (repo / "sub/file").write_text("AFTER!\n")
    (repo / "first").write_text("ordinary change\n")
    result = tool_type(repo).execute({})
    assert result.success, result
    assert not marker.exists()


@pytest.mark.skipif(
    not (
        (sys.platform == "darwin" and os.getenv("RUN_SANDBOX_NATIVE_TESTS") == "1")
        or (sys.platform == "linux" and os.getenv("RUN_SANDBOX_LINUX_TESTS") == "1")
    ),
    reason="Opt-in real native sandbox enforcement",
)
@pytest.mark.parametrize(
    "name,arguments",
    [
        ("git_diff", {"mode": "working"}),
        ("git_diff", {"mode": "head"}),
        ("git_status", {}),
    ],
)
@pytest.mark.parametrize("driver", ["clean", "process"])
def test_native_filters_cannot_write_outside_workspace(
    repository,
    tmp_path,
    name,
    arguments,
    driver,
):
    from sandbox.native import NativeBackend

    repo, git = repository
    inside, outside = repo / "filter-ran", tmp_path / "outside-marker"
    (repo / ".gitattributes").write_text("first filter=probe\n")
    (repo / "first").write_text("FIRST\n")
    code = (
        "import sys\nfrom pathlib import Path\n"
        f"Path({str(inside)!r}).write_text('ran')\n"
        f"try: Path({str(outside)!r}).write_text('escaped')\n"
        "except PermissionError: pass\n"
    )
    if driver == "clean":
        code += "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    # A process driver exits after the marker checks; no protocol handshake is
    # needed to verify inheritance of the sandbox's filesystem restrictions.
    git("config", f"filter.probe.{driver}", shlex.join([sys.executable, "-c", code]))
    backend = NativeBackend(repo)
    try:
        result = backend.execute(repo, name, arguments)
        assert result.error_code != "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX", result
        assert inside.exists(), result
        assert not outside.exists(), result
        assert backend.healthy, result
    finally:
        backend.close()
