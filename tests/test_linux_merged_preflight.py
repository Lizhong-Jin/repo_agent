"""One workspace traversal and one sandbox for isolation/CUDA startup checks."""

import errno
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest
import test_native_performance_metrics as metrics_tests
from test_linux_native_gpu import result, startup_report

from sandbox import linux_policy as policy_module
from sandbox import linux_preflight
from sandbox.linux_gpu import NativeGPU
from tools._internal.process_runner import ProcessResult, ProcessRunner

backend = metrics_tests.backend


def success():
    return ProcessResult(0, "ok", "", False, None, 1, False, False)


@pytest.mark.parametrize("worker", [False, True])
def test_real_launch_path_enumerates_workspace_once_including_masked_trees(
    backend,
    monkeypatch,
    worker,
):
    files = []
    for name in ("src", "logs", ".git/objects", ".venv/lib"):
        directory = backend.workspace / name
        directory.mkdir(parents=True)
        path = directory / "ordinary"
        path.touch()
        files.append(path)
    backend.read_paths = (*backend.read_paths, backend.workspace / ".venv")
    visits, checks = Counter(), Counter()
    scandir, check = policy_module.open_directory, backend._check_workspace_file

    def enumerate_directory(path):
        if not isinstance(path, int):
            visits[Path(path)] += 1
        return scandir(path)

    def validate(path, info):
        checks[Path(path)] += 1
        check(path, info)

    monkeypatch.setattr(policy_module, "open_directory", enumerate_directory)
    monkeypatch.setattr(backend, "_check_workspace_file", validate)
    monkeypatch.setattr(backend, "_check_workspace", lambda: pytest.fail("Separate workspace walk"))
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: success())
    if worker:
        backend._run(request={"name": "git_status", "arguments": {}}, git_read=True)
    else:
        backend.execute(backend.workspace, "run_command", {"command": ["/usr/bin/true"]})
    workspace_visits = {
        path: count for path, count in visits.items() if path.is_relative_to(backend.workspace)
    }
    assert len(workspace_visits) == 7
    assert set(workspace_visits.values()) == {1}
    assert checks == Counter({path: 1 for path in files})
    assert backend.last_policy_metrics["workspace_entries_checked"] == len(files)
    assert backend.last_policy_metrics["workspace_directories_scanned"] == 7


@pytest.mark.parametrize("parent", ["src", "logs", ".git/objects", ".venv/lib", "private"])
@pytest.mark.parametrize("kind", ["hardlink", "fifo", "socket"])
def test_unsafe_entries_in_masked_and_readonly_trees_prevent_launch(
    backend,
    monkeypatch,
    parent,
    kind,
):
    directory = backend.workspace / parent
    directory.mkdir(parents=True)
    path = directory / "ordinary"
    if kind == "hardlink":
        outside = backend.directory / "outside"
        outside.touch()
        path.hardlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        import stat

        path.touch()
        original = os.lstat

        def socket_metadata(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if Path(candidate) == path:
                return os.stat_result((stat.S_IFSOCK | 0o600, *tuple(info)[1:]))
            return info

        monkeypatch.setattr(os, "lstat", socket_metadata)
    backend.protected_paths = (backend.workspace / "private",)
    backend.read_paths = (*backend.read_paths, backend.workspace / ".venv")
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: pytest.fail("Unsafe launch"))
    with pytest.raises(ValueError, match="硬链接|特殊文件"):
        backend._run(["/usr/bin/true"])
    assert not backend.last_run_metrics["complete"]


def test_workspace_metadata_is_fresh_on_every_process_launch(backend, monkeypatch):
    path = backend.workspace / "ordinary"
    path.touch()
    calls = []
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: calls.append(1) or success())
    backend._run(["/usr/bin/true"])
    (backend.directory / "outside").hardlink_to(path)
    with pytest.raises(ValueError, match="硬链接"):
        backend._run(["/usr/bin/true"])
    assert calls == [1]


def test_masked_workspace_directory_permission_error_still_prevents_launch(backend, monkeypatch):
    hidden = backend.workspace / "logs"
    hidden.mkdir()
    original = os.scandir
    original_open = policy_module.open_directory

    def opened(path):
        if not isinstance(path, int) and Path(path) == hidden:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original_open(path)

    def scandir(path):
        if isinstance(path, int):
            return original(path)
        if not isinstance(path, int) and Path(path) == hidden:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(policy_module, "open_directory", opened)
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: pytest.fail("Unsafe launch"))
    with pytest.raises(PermissionError):
        backend._run(["/usr/bin/true"])


def test_protected_workspace_root_is_still_validated(backend, monkeypatch):
    backend.protected_paths = (backend.workspace,)
    os.mkfifo(backend.workspace / "fifo")
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: pytest.fail("Unsafe launch"))
    with pytest.raises(ValueError, match="特殊文件"):
        backend._run(["/usr/bin/true"])


@pytest.mark.parametrize("gpu", [False, True])
def test_controller_uses_separate_children_and_deadlines_in_one_sandbox(monkeypatch, tmp_path, gpu):
    calls = []

    def run_check(command, timeout):
        calls.append((command, timeout))
        return startup_report()["isolation"]

    monkeypatch.setattr(linux_preflight, "run_check", run_check)
    report = linux_preflight.probe(tmp_path, tmp_path / "denied", gpu=gpu)
    assert calls[0][1] == 30 and "--isolation-only" in calls[0][0]
    assert len(calls) == (2 if gpu else 1)
    assert set(report) == ({"isolation", "gpu"} if gpu else {"isolation"})
    if gpu:
        assert calls[1][1] == 90 and calls[1][0][-1].endswith("native_gpu_probe.py")
    assert all(command[1] == "-I" and "bwrap" not in command for command, _ in calls)


@pytest.mark.parametrize("failure", ["exit", "timeout", "invalid_stdout"])
def test_isolation_failure_never_runs_cuda_probe(monkeypatch, tmp_path, failure):
    calls = []
    check = startup_report()["isolation"]
    if failure == "exit":
        check["exit_code"] = 1
    elif failure == "timeout":
        check.update(exit_code=None, timed_out=True)
    else:
        check["stdout"] = "invalid"

    def run_check(command, timeout):
        calls.append(command)
        return check

    monkeypatch.setattr(linux_preflight, "run_check", run_check)
    assert "gpu" not in linux_preflight.probe(tmp_path, tmp_path / "denied", gpu=True)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "start"])
def test_child_failure_preserves_phase_error_and_duration(monkeypatch, failure):
    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        raise OSError("cannot start")

    monkeypatch.setattr(subprocess, "run", run)
    check = linux_preflight.run_check(["never-executed"], 30)
    assert check["exit_code"] is None and check["stderr"]
    assert check["timed_out"] == (failure == "timeout")
    assert check["duration_ms"] >= 0


@pytest.mark.parametrize(
    "failure",
    [
        "isolation",
        "outer_timeout",
        "truncated",
        "cleanup",
        "missing_isolation",
        "invalid_json",
    ],
)
def test_host_rejects_invalid_combined_isolation_report(backend, monkeypatch, failure):
    backend.gpu = NativeGPU("nvidia", "all", ())
    report = startup_report(gpu={"cuda_kernel_verified": True, "devices": ["GPU-test"]})
    check = result("")
    if failure == "isolation":
        report["isolation"]["exit_code"] = 1
    elif failure == "outer_timeout":
        check.timed_out = True
    elif failure == "truncated":
        check.stdout_truncated = True
    elif failure == "cleanup":
        backend.healthy = False
    elif failure == "missing_isolation":
        del report["isolation"]
    check.stdout = "invalid" if failure == "invalid_json" else json.dumps(report)
    monkeypatch.setattr(backend, "_run", lambda *a, **kw: check)
    with pytest.raises(ValueError, match="原生沙箱自检失败"):
        backend._preflight()


@pytest.mark.parametrize("failure", ["missing", "timeout", "exit", "invalid_report"])
def test_host_classifies_cuda_failure_after_successful_isolation(backend, monkeypatch, failure):
    backend.gpu = NativeGPU("nvidia", "all", ())
    report = startup_report(gpu={"cuda_kernel_verified": True, "devices": ["GPU-test"]})
    if failure == "missing":
        del report["gpu"]
    elif failure == "timeout":
        report["gpu"].update(exit_code=None, timed_out=True)
    elif failure == "exit":
        report["gpu"]["exit_code"] = 1
    else:
        report["gpu"]["stdout"] = "not-json"
    monkeypatch.setattr(backend, "_run", lambda *a, **kw: result(json.dumps(report)))
    with pytest.raises(ValueError, match="CUDA 自检失败"):
        backend._preflight()
