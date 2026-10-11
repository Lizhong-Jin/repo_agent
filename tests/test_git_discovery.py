"""Repository discovery must distinguish OS launch failures from Git errors."""

from types import SimpleNamespace

import pytest

from host_support.processes import ProcessResult
from tools.git_tools import GitDiffTool, GitStatusTool


@pytest.mark.parametrize("tool_type", [GitDiffTool, GitStatusTool])
@pytest.mark.parametrize(
    "exit_code,stderr,timeout,cleanup,error_code",
    [
        (3221225794, "", False, None, "GIT_ERROR"),
        (-1073741502, "", False, None, "GIT_ERROR"),
        (
            128,
            "fatal: not a git repository (or any parent): .git",
            False,
            None,
            "NOT_A_GIT_REPOSITORY",
        ),
        (128, "fatal: detected dubious ownership in repository", False, None, "GIT_ERROR"),
        (None, "", True, None, "GIT_TIMEOUT"),
        (0, "", False, "job still active", "GIT_CLEANUP_FAILED"),
    ],
)
def test_discovery_preserves_failure_diagnostics(
    tmp_path, tool_type, exit_code, stderr, timeout, cleanup, error_code
):
    process = ProcessResult(
        exit_code=exit_code,
        stdout="",
        stderr=stderr,
        timed_out=timeout,
        cleanup_error=cleanup,
        cleanup_status="unknown" if cleanup else "confirmed",
        duration_ms=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    tool = tool_type(tmp_path)
    tool.runner = SimpleNamespace(run=lambda *args, **kwargs: process)
    result = tool.execute({})
    assert not result.success and result.error_code == error_code
    assert result.data["exit_code"] == exit_code
    assert result.data["stderr"] == stderr
    assert result.data["cleanup_error"] == cleanup
    if exit_code in (3221225794, -1073741502):
        assert result.data["exit_code_hex"] == "0xC0000142"


def test_discovery_bounds_diagnostics(tmp_path):
    process = ProcessResult(
        exit_code=1,
        stdout="x" * 5000,
        stderr="y" * 5000,
        timed_out=False,
        cleanup_error=None,
        duration_ms=0,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    tool = GitStatusTool(tmp_path)
    tool.runner = SimpleNamespace(run=lambda *args, **kwargs: process)
    result = tool.execute({})
    assert len(result.data["stdout"]) == len(result.data["stderr"]) == 4096
    assert result.data["stdout_truncated"] and result.data["stderr_truncated"]
