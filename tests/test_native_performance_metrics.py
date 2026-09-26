"""Native preparation is timed separately from process execution and file tools."""

import json
from pathlib import Path

import pytest

from sandbox.linux_gpu import NativeGPU
from sandbox.linux_native import LinuxNativeBackend
from tools._internal.process_runner import ProcessResult, ProcessRunner


@pytest.fixture
def backend(tmp_path):
    backend = object.__new__(LinuxNativeBackend)
    backend.workspace = tmp_path / "workspace"
    backend.workspace.mkdir()
    backend.directory = tmp_path / "control"
    backend.directory.mkdir()
    backend.runtime = tmp_path / "runtime"
    backend.runtime.mkdir()
    backend.read_paths = (backend.runtime,)
    backend.protected_paths = ()
    backend.python = Path("/usr/bin/python3")
    backend.executable = Path("/usr/bin/bwrap")
    backend.healthy = True
    return backend


def outcome(text="ok"):
    return ProcessResult(0, text, "", False, None, 1, False, False)


@pytest.mark.parametrize("worker", [False, True])
def test_preparation_process_and_cleanup_are_timed_separately(backend, monkeypatch, worker):
    invocations = []

    def run(self, command, **kwargs):
        invocations.append(command)
        assert command[0] == "/usr/bin/bwrap"
        return outcome()

    monkeypatch.setattr(ProcessRunner, "run", run)
    if worker:
        backend._run(request={"name": "git_status", "arguments": {}}, git_read=True)
    else:
        backend._run(["/usr/bin/true"])
    metrics = backend.last_run_metrics
    assert metrics["complete"] is True
    assert metrics["total_ms"] >= metrics["preparation_ms"] + metrics["process_ms"]
    assert metrics["preparation_ms"] >= metrics["policy"]["scan_ms"]
    assert metrics["policy"]["complete"] is True
    assert metrics["policy"]["materialization_ms"] >= 0
    assert not list(backend.directory.iterdir())  # Includes per-call scratch cleanup.
    assert len(invocations) == 1
    assert backend._policy_plan().read_paths == (backend.runtime,)


def test_failed_policy_has_metrics_but_never_starts_a_process(backend, monkeypatch):
    (backend.workspace / ".env").symlink_to("missing")
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: pytest.fail("launched"))
    with pytest.raises(ValueError, match="符号链接"):
        backend._run(["/usr/bin/true"])
    metrics = backend.last_run_metrics
    assert not metrics["complete"] and not metrics["policy"]["complete"]
    assert metrics["preparation_ms"] > 0 and metrics["process_ms"] == 0


def test_file_tool_has_no_process_or_scan_and_does_not_reuse_previous_metrics(backend, monkeypatch):
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: outcome())
    result = backend.execute(backend.workspace, "run_command", {"command": ["/usr/bin/true"]})
    assert result.success
    command_metrics = backend.last_tool_metrics
    assert len(command_metrics["runs"]) == 1 and command_metrics["workspace_check_ms"] == 0
    assert command_metrics["runs"][0]["policy"]["workspace_directories_scanned"] == 1
    (backend.workspace / "hello.txt").write_text("hello")
    result = backend.execute(backend.workspace, "read_file", {"reads": [{"path": "hello.txt"}]})
    assert result.success
    assert backend.last_tool_metrics["runs"] == []
    assert backend.last_tool_metrics["workspace_check_ms"] == 0
    assert command_metrics["runs"]  # Earlier snapshot remains intact.


@pytest.mark.parametrize("gpu", [False, True])
def test_startup_retains_separate_isolation_and_gpu_probe_timings(tmp_path, monkeypatch, gpu):
    def platform_setup(self):
        self.executable = Path("/usr/bin/bwrap")
        self.gpu = NativeGPU("nvidia", "all", ()) if gpu else None

    monkeypatch.setattr(LinuxNativeBackend, "_platform_setup", platform_setup)
    monkeypatch.setattr(LinuxNativeBackend, "_read_paths", lambda self: (self.runtime,))
    calls = []

    def run(self, command, **kwargs):
        calls.append(command)
        from test_linux_native_gpu import startup_report

        report = startup_report(gpu={"cuda_kernel_verified": True, "devices": ["GPU-test"]}
                                if "--gpu" in command else None)
        return outcome(json.dumps(report))

    monkeypatch.setattr(ProcessRunner, "run", run)
    backend = LinuxNativeBackend(tmp_path)
    try:
        metrics = backend.startup_metrics
        assert len(calls) == len(metrics["runs"]) == 1
        assert metrics["runtime_copy_ms"] > 0 and metrics["workspace_check_ms"] == 0
        assert metrics["total_ms"] >= sum(item["total_ms"] for item in metrics["runs"])
        assert all(item["policy"]["directories_scanned"] > 0 for item in metrics["runs"])
        assert all(item["complete"] for item in metrics["runs"])
        assert metrics["checks"]["isolation_ms"] == 1
        assert ("gpu_ms" in metrics["checks"]) == gpu
    finally:
        backend.close()
