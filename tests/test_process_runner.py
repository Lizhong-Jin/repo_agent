import sys

import pytest

from tools import ProcessResult, ProcessRunner, ProcessStartError, RunCommandTool
from host_support import processes as process_module


def test_runner_reuses_configuration_with_independent_cwd_and_output(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runner = ProcessRunner(base_env={"RUNNER_MESSAGE": "configured"})

    for directory in (first, second):
        result = runner.run(
            (
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                'print(Path.cwd().name, os.environ["RUNNER_MESSAGE"]); '
                'Path("created.txt").write_text("ok")',
            ),
            cwd=directory,
            timeout_seconds=5,
        )
        assert isinstance(result, ProcessResult)
        assert result.stdout == f"{directory.name} configured\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert not result.timed_out
        assert (directory / "created.txt").read_text() == "ok"


@pytest.mark.parametrize(
    ("command", "timeout"),
    [
        ("echo text", 1),
        ([], 1),
        ([""], 1),
        (["echo", None], 1),
        (["echo", "bad\x00arg"], 1),
        (["echo"], True),
        (["echo"], 0),
    ],
)
def test_runner_rejects_invalid_calls_without_starting_process(
    tmp_path, monkeypatch, command, timeout
):
    def unexpected_start(*args, **kwargs):
        pytest.fail("Invalid runner input must not launch a process")

    monkeypatch.setattr(process_module.subprocess, "Popen", unexpected_start)
    with pytest.raises(ValueError):
        ProcessRunner().run(command, cwd=tmp_path, timeout_seconds=timeout)


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (FileNotFoundError("test executable"), "FILE_NOT_FOUND"),
        (PermissionError("test permission"), "PERMISSION_DENIED"),
        (OSError("test startup failure"), "PROCESS_START_ERROR"),
    ],
)
def test_start_failure_is_independent_of_tool_error_mapping(
    tmp_path, monkeypatch, failure, expected_code
):
    def fail_start(*args, **kwargs):
        raise failure

    monkeypatch.setattr(process_module.subprocess, "Popen", fail_start)
    with pytest.raises(ProcessStartError) as error:
        ProcessRunner().run(["executable"], cwd=tmp_path)
    assert error.value.cause is failure
    assert error.value.__cause__ is failure

    result = RunCommandTool(tmp_path, execution_allowed=True).execute({"command": ["executable"]})
    assert not result.success
    assert result.error_code == expected_code


@pytest.mark.parametrize(
    ("cwd", "expected_code"),
    [("../", "PATH_OUTSIDE_WORKSPACE"), (".env.local", "PROTECTED_FILE")],
)
def test_tool_checks_workspace_policy_before_calling_runner(
    tmp_path, monkeypatch, cwd, expected_code
):
    tool = RunCommandTool(tmp_path, execution_allowed=True)

    def unexpected_run(*args, **kwargs):
        pytest.fail("Rejected tool paths must not reach ProcessRunner")

    monkeypatch.setattr(tool.runner, "run", unexpected_run)
    result = tool.execute({"command": [sys.executable, "-c", "pass"], "cwd": cwd})
    assert result.error_code == expected_code


def test_tool_preserves_result_contract_when_delegating(tmp_path, monkeypatch):
    directory = tmp_path / "nested"
    directory.mkdir()
    tool = RunCommandTool(tmp_path, execution_allowed=True)

    def run(command, *, cwd, timeout_seconds):
        assert command == ["example", "argument"]
        assert cwd == directory.resolve()
        assert timeout_seconds == 7
        return ProcessResult(
            exit_code=None,
            stdout="out",
            stderr="err",
            timed_out=True,
            cleanup_error="cleanup incomplete",
            duration_ms=7250,
            stdout_truncated=True,
            stderr_truncated=False,
            status="timed_out",
            cleanup_status="unknown",
            output_complete=False,
        )

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute(
        {"command": ["example", "argument"], "cwd": "nested", "timeout_seconds": 7}
    )

    assert result.success
    assert result.data == {
        "command": ["example", "argument"],
        "cwd": "nested",
        "exit_code": None,
        "stdout": "out",
        "stderr": "err",
        "timed_out": True,
        "cleanup_error": "cleanup incomplete",
        "duration_ms": 7250,
        "stdout_truncated": True,
        "stderr_truncated": False,
        "status": "timed_out",
        "cleanup_status": "unknown",
        "output_complete": False,
        "pid": None,
        "process_group_id": None,
        "cleanup_diagnostics": [],
    }
