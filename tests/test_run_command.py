import json
import os
import signal
import sys
import time
from types import MappingProxyType

import pytest

from tools._internal import process_runner as process_module
from tools._internal.process_runner import ProcessRunner
from tools.execute import RunCommandTool


def run_python(tool, code, **arguments):
    return tool.execute({"command": [sys.executable, "-c", code], **arguments})


@pytest.mark.parametrize("exit_code", [0, 3])
def test_normal_exit_preserves_both_streams(tmp_path, exit_code):
    result = run_python(
        RunCommandTool(tmp_path, execution_allowed=True),
        f'import sys; print("out"); print("err", file=sys.stderr); sys.exit({exit_code})',
    )

    assert result.success
    assert result.data["exit_code"] == exit_code
    assert result.data["stdout"] == "out\n"
    assert result.data["stderr"] == "err\n"
    assert result.data["timed_out"] is False


def test_large_outputs_are_drained_and_bounded(tmp_path):
    result = run_python(
        RunCommandTool(tmp_path, execution_allowed=True, max_output_bytes=128),
        'import os; os.write(1, b"A" * 200000); os.write(2, b"B" * 200000)',
        timeout_seconds=5,
    )

    assert result.success
    assert result.data["exit_code"] == 0
    for stream, char in [("stdout", "A"), ("stderr", "B")]:
        assert result.data[f"{stream}_truncated"]
        assert result.data[stream] == char * 64 + "\n... (output truncated) ...\n" + char * 64


@pytest.fixture
def launched_processes(monkeypatch):
    """Retain exact process handles so a failed test can clean up its own group."""
    processes = []
    original = process_module.subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(process_module.subprocess, "Popen", launch)
    yield processes
    for process in processes:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # A host may reject signaling an already vanished group.
                if process.poll() is None:
                    process.kill()
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=2)


def test_timeout_keeps_partial_output_and_reaps_leader(tmp_path, launched_processes):
    result = run_python(
        RunCommandTool(tmp_path, execution_allowed=True),
        'import time; print("started", flush=True); time.sleep(8)',
        timeout_seconds=1,
    )

    assert result.success
    assert result.data["timed_out"]
    assert result.data["exit_code"] is None
    assert result.data["stdout"] == "started\n"
    assert result.data["duration_ms"] < 4500
    assert launched_processes[0].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group termination")
@pytest.mark.parametrize("leader_exits", [False, True])
def test_inherited_pipes_and_stubborn_child_share_deadline(
    tmp_path, launched_processes, leader_exits
):
    # The child ignores TERM and continually updates a marker. It also exits
    # after 8s as a backstop if the termination behavior regresses.
    child = """
import signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('ready').touch()
deadline = time.monotonic() + 8
while time.monotonic() < deadline:
    Path('heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(0.02)
"""
    parent = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "while not Path('ready').exists(): time.sleep(0.01)\n"
        "print('child ready', flush=True)\n" + ("" if leader_exits else "time.sleep(8)\n")
    )

    result = run_python(RunCommandTool(tmp_path, execution_allowed=True), parent, timeout_seconds=1)

    assert result.success
    assert result.data["timed_out"]
    assert result.data["stdout"] == "child ready\n"
    assert result.data["duration_ms"] < 4500
    assert launched_processes[0].poll() is not None
    before = (tmp_path / "heartbeat").read_bytes()
    time.sleep(0.15)
    assert (tmp_path / "heartbeat").read_bytes() == before
    assert launched_processes[0].stdout.closed
    assert launched_processes[0].stderr.closed


@pytest.mark.skipif(os.name != "posix", reason="POSIX detached sessions")
def test_escaped_descendant_cannot_keep_output_collection_open(tmp_path, launched_processes):
    child = "import time; time.sleep(8)"
    parent = (
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
        "Path('escaped.pid').write_text(str(child.pid))\n"
    )
    try:
        result = run_python(RunCommandTool(tmp_path, execution_allowed=True), parent, timeout_seconds=1)
        assert result.success
        assert result.data["timed_out"]
        assert result.data["duration_ms"] < 4500
        assert result.data["cleanup_error"]
        assert launched_processes[0].stdout.closed
        assert launched_processes[0].stderr.closed
    finally:
        pid_file = tmp_path / "escaped.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cancellation_terminates_command_and_closes_pipes(
    tmp_path, monkeypatch, launched_processes
):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ProcessRunner, "_collect_output", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_python(RunCommandTool(tmp_path, execution_allowed=True), "import time; time.sleep(8)")

    process = launched_processes[0]
    assert process.poll() is not None
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize(
    "environment",
    [
        [],
        [("KEY", "value")],
        "KEY=value",
        1,
        {1: "value"},
        {b"KEY": "value"},
        {"": "value"},
        {"BAD=KEY": "value"},
        {"BAD\x00KEY": "value"},
        {"KEY": None},
        {"KEY": 1},
        {"KEY": b"value"},
        {"KEY": "PRIVATE_VALUE\x00suffix"},
        {"KEY": "\ud800"},
    ],
)
def test_invalid_environment_is_rejected_at_construction(tmp_path, environment):
    with pytest.raises(ValueError, match="base_env") as error:
        RunCommandTool(tmp_path, execution_allowed=True, base_env=environment)
    assert "PRIVATE_VALUE" not in str(error.value)


def test_environment_snapshot_supports_empty_and_unicode_values(tmp_path):
    environment = {"COMMAND_TEST": "中文", "COMMAND_EMPTY": ""}
    tool = RunCommandTool(tmp_path, execution_allowed=True, base_env=MappingProxyType(environment))
    environment["COMMAND_TEST"] = "changed"

    result = run_python(
        tool,
        'import json, os; print(json.dumps([os.environ["COMMAND_TEST"], '
        'os.environ["COMMAND_EMPTY"]]))',
    )

    assert result.success
    assert json.loads(result.data["stdout"]) == ["中文", ""]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group permission failure")
def test_group_signal_denial_is_reported_and_leader_is_reaped(
    tmp_path, monkeypatch, launched_processes
):
    def deny_group_signal(*args):
        raise PermissionError("simulated signal restriction")

    with monkeypatch.context() as scoped:
        scoped.setattr(process_module.os, "killpg", deny_group_signal)
        result = run_python(
            RunCommandTool(tmp_path, execution_allowed=True), "import time; time.sleep(8)", timeout_seconds=1
        )

    assert result.success
    assert result.data["timed_out"]
    assert result.data["cleanup_error"]
    assert result.data["duration_ms"] < 4500
    assert launched_processes[0].poll() is not None


def test_explicit_empty_environment_does_not_inherit_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMAND_PRIVATE_TEST", "PRIVATE_VALUE")
    result = run_python(
        RunCommandTool(tmp_path, execution_allowed=True, base_env={}),
        'import os; print("COMMAND_PRIVATE_TEST" in os.environ)',
    )

    assert result.success
    assert result.data["stdout"] == "False\n"
