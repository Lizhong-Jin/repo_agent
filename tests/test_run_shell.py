"""Shell syntax, process lifecycle and executor-platform boundaries."""

from types import SimpleNamespace

import pytest

from sandbox.session import SandboxedTool
from sandbox.worker import execute_request
from sandbox.writeback import WritebackGuard
from tools import ExecutionKind, GetExecutionEnvironmentTool, RunCommandTool, RunShellTool
from tools import execute as execution
from tools._internal.process_runner import ProcessResult


@pytest.fixture
def shell(tmp_path):
    if execution._bash_executable() is None:
        pytest.skip("requires system Bash on Linux/macOS")
    return RunShellTool(tmp_path, execution_allowed=True)


def test_multiline_pipeline_redirection_and_quoted_paths(shell, tmp_path):
    nested = tmp_path / "space 中文"
    nested.mkdir()
    result = shell.execute(
        {
            "script": "for value in one two; do printf '%s\\n' \"$value\"; done | "
            "tr 'a-z' 'A-Z' > 'result space.txt'\ncat 'result space.txt'",
            "cwd": nested.name,
        }
    )
    assert result.success and result.data["exit_code"] == 0, result
    assert result.data["stdout"] == "ONE\nTWO\n"
    assert result.data["shell"] == "bash"
    assert result.data["cwd"] == nested.name
    assert result.data["cleanup_status"] == "confirmed"
    assert (nested / "result space.txt").read_text() == "ONE\nTWO\n"


def test_pipefail_without_implicit_errexit_or_nounset(shell):
    failed = shell.execute({"script": "exit 7 | cat"})
    assert failed.success and failed.data["exit_code"] == 7, failed
    normal = shell.execute({"script": 'false; printf "%s" "${ABSENT_VALUE}"; printf done'})
    assert normal.data["exit_code"] == 0 and normal.data["stdout"] == "done"


def test_startup_files_and_inherited_functions_are_not_evaluated(shell, tmp_path):
    startup = tmp_path / "startup"
    startup.write_text("printf injected; touch startup-ran")
    # A caller-supplied environment must also be filtered, without mutating the caller.
    environment = {
        **shell.base_env,
        "BASH_ENV": str(startup),
        "ENV": str(startup),
        "SHELLOPTS": "errexit",
        "BASH_FUNC_injected%%": "() { echo bad; }",
    }
    tool = RunShellTool(tmp_path, execution_allowed=True, base_env=environment)
    assert environment["BASH_ENV"] == str(startup)
    assert "BASH_ENV" not in tool.runner.base_env
    result = tool.execute({"script": "type injected >/dev/null 2>&1; printf clean"})
    assert result.data["stdout"] == "clean"
    assert not (tmp_path / "startup-ran").exists()
    # Native adapters provide their own env: -p is a second startup-file safeguard.
    tool.runner.base_env["BASH_ENV"] = str(startup)
    assert tool.execute({"script": "printf clean"}).data["stdout"] == "clean"
    assert not (tmp_path / "startup-ran").exists()


def test_no_state_persists_and_large_script_has_its_own_limit(shell):
    assert shell.execute({"script": "export SHELL_TEST_VALUE=old; cd /"}).data["exit_code"] == 0
    result = shell.execute({"script": "#" + "x" * 5000 + '\nprintf "%s" "${SHELL_TEST_VALUE-new}"'})
    assert result.data["stdout"] == "new"
    assert result.data["cwd"] == "."


@pytest.mark.parametrize(
    "arguments,code",
    [
        (None, "INVALID_ARGUMENTS"),
        ({}, "INVALID_ARGUMENTS"),
        ({"script": " "}, "INVALID_ARGUMENTS"),
        ({"script": []}, "INVALID_ARGUMENTS"),
        ({"script": "x\x00"}, "INVALID_ARGUMENTS"),
        ({"script": "\ud800"}, "UNSUPPORTED_ENCODING"),
        ({"script": "echo", "command": ["echo"]}, "INVALID_ARGUMENTS"),
        ({"script": "echo", "shell": "powershell"}, "INVALID_ARGUMENTS"),
        ({"script": "echo", "timeout_seconds": True}, "INVALID_ARGUMENTS"),
        ({"script": "echo", "timeout_seconds": 121}, "INVALID_ARGUMENTS"),
        ({"script": "echo", "cwd": "../"}, "PATH_OUTSIDE_WORKSPACE"),
        ({"script": "echo", "cwd": ".env"}, "PROTECTED_FILE"),
    ],
)
def test_invalid_input_never_launches(tmp_path, monkeypatch, arguments, code):
    tool = RunShellTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(execution.sys, "platform", "linux")
    monkeypatch.setattr(execution, "_bash_executable", lambda: "/bin/bash")
    monkeypatch.setattr(tool.runner, "run", lambda *a, **kw: pytest.fail("unexpected execution"))
    assert tool.execute(arguments).error_code == code


def test_script_limit_is_bytes_and_local_execution_is_denied(tmp_path):
    tool = RunShellTool(tmp_path, execution_allowed=True, max_script_bytes=5)
    assert tool.execute({"script": "中文"}).error_code == "SCRIPT_TOO_LARGE"
    assert RunShellTool(tmp_path).execute({"script": "echo"}).error_code == "SANDBOX_REQUIRED"
    with pytest.raises(ValueError, match="max_script_bytes"):
        RunShellTool(tmp_path, max_script_bytes=True)


def test_platform_is_checked_on_execution_not_schema_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "win32")
    tool = RunShellTool(tmp_path, execution_allowed=True)
    assert tool.definition.name == "run_shell"
    assert tool.execute({"script": "echo hello"}).error_code == "SHELL_UNSUPPORTED_PLATFORM"
    report = (
        GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
        .execute({"sections": ["execution"]})
        .data["execution"]
    )
    assert report["shell"]["reason"] == "unsupported_platform"
    assert not report["shell_execution_allowed"]
    # An unsupported shell must not disable argv execution.
    command = RunCommandTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(
        command.runner,
        "run",
        lambda *a, **kw: ProcessResult(0, "ok", "", False, None, 1, False, False),
    )
    assert command.execute({"command": ["program.exe"]}).data["exit_code"] == 0


def test_missing_bash_does_not_fall_back_to_sh(tmp_path, monkeypatch):
    monkeypatch.setattr(execution.sys, "platform", "linux")
    monkeypatch.setattr(execution, "_bash_executable", lambda: None)
    tool = RunShellTool(tmp_path, execution_allowed=True)
    assert tool.execute({"script": "printf x"}).error_code == "SHELL_UNAVAILABLE"


def test_timeout_returns_partial_output_and_cleans_pipeline(shell):
    result = shell.execute(
        {"script": "printf before; printf warning >&2; sleep 30 | cat", "timeout_seconds": 1}
    )
    assert result.success and result.data["timed_out"], result
    assert result.data["stdout"] == "before"
    assert result.data["stderr"] == "warning"
    assert not result.data["output_complete"]
    assert result.data["cleanup_status"] == "confirmed", result
    assert not result.data["cleanup_error"]


def test_successful_shell_cleans_observed_background_children(shell, tmp_path):
    result = shell.execute({"script": "sleep 30 >/dev/null 2>&1 &\necho $! > child.pid\nsleep 0.2"})
    assert result.data["exit_code"] == 0 and result.data["cleanup_status"] == "confirmed", result
    from host_support.supervision import ProcessTable

    child = ProcessTable().snapshot().get(int((tmp_path / "child.pid").read_text()))
    assert child is None or child.zombie


def test_bounded_output(shell):
    shell.runner.max_output_bytes = 128
    result = shell.execute({"script": "printf '%2000s' x; printf '%2000s' y >&2"})
    assert result.data["stdout_truncated"] and result.data["stderr_truncated"]
    assert not result.data["output_complete"]
    assert len(result.data["stdout"]) < 400


def test_cancellation_propagates_after_cleanup(shell, monkeypatch):
    def cancel(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(shell.runner, "_collect_output", cancel)
    with pytest.raises(KeyboardInterrupt):
        shell.execute({"script": "sleep 30"})
    assert shell.runner.last_cleanup_status == "confirmed"


def test_worker_executes_shell_through_existing_protocol(shell, tmp_path, capsys):
    import json

    execute_request({"name": "run_shell", "arguments": {"script": "printf worker"}}, tmp_path)
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] and payload["data"]["stdout"] == "worker"


def test_windows_host_proxy_keeps_linux_shell_and_validation_tracking(tmp_path, monkeypatch):
    calls = []
    outcome = [1]

    def execute(workspace, name, arguments):
        calls.append((name, arguments))
        return execution.ToolResult(True, {"exit_code": outcome[0]})

    session = SimpleNamespace(
        workspace=tmp_path, backend=SimpleNamespace(execute=execute), guard=WritebackGuard()
    )
    monkeypatch.setattr(execution.sys, "platform", "win32")
    proxy = SandboxedTool(
        RunShellTool(tmp_path).definition, session, execution_kind=ExecutionKind.SANDBOXED_PROCESS
    )
    assert "check_id" in proxy.definition.parameters["properties"]
    proxy.execute({"script": "false", "check_id": "build"})
    assert session.guard.pending
    outcome[0] = 0
    proxy.execute({"script": "true", "check_id": "build"})
    assert not session.guard.pending
    assert calls == [("run_shell", {"script": "false"}), ("run_shell", {"script": "true"})]
    assert proxy.execute({"script": "true", "check_id": "bad id"}).error_code == "INVALID_ARGUMENTS"
    assert len(calls) == 2
