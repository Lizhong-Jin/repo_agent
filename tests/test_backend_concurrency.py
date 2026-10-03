"""Backend contracts apply even without an AgentRuntime or ToolScheduler."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from sandbox.concurrency import BackendGate
from sandbox.docker import DockerBackend
from sandbox.native_common import NativeBackendBase
from sandbox.policy import SandboxPolicy
from sandbox.policy_scan import PolicyPlan, ScanRequest
from sandbox.rust_policy import RustPolicyScanner
from sandbox.session import SandboxedTool
from tools import ExecutionKind, ToolResult
from tools._internal.file_policy import PROTECTED_NAME_RULES
from tools._internal.process_runner import ProcessResult
from tools.scheduling import SERIAL, scheduling_policy_of


def native(tmp_path):
    backend = object.__new__(NativeBackendBase)
    backend.workspace = tmp_path
    backend.healthy = True
    backend.read_paths = ()
    return backend


def test_native_proxies_preserve_read_policy_and_docker_proxies_restrict_it(tmp_path):
    backend = native(tmp_path)
    proxies = backend.tools()
    assert {t.definition.name for t in proxies if scheduling_policy_of(t).parallel} == {
        "read_file",
        "list_files",
        "find_files",
        "search_files",
        "get_path_info",
    }
    for tool in proxies:
        proxy = SandboxedTool(tool.definition, None, execution_kind=tool.execution_kind)
        assert scheduling_policy_of(proxy) == SERIAL


def test_native_read_calls_have_separate_metrics_and_file_access(tmp_path, monkeypatch):
    backend = native(tmp_path)
    rendezvous = Barrier(2)
    original = backend._execute_file
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")

    def execute(tool, arguments):
        marker = arguments["reads"][0]["path"]
        backend._set_metric("last_workspace_check_ms", marker)
        backend._set_metric("last_policy_metrics", {"marker": marker})
        metrics = backend._active_metrics()
        metrics["runs"].append({"marker": marker})
        rendezvous.wait(3)
        assert backend._get_metric("last_workspace_check_ms") == marker
        assert backend._get_metric("last_policy_metrics") == {"marker": marker}
        assert metrics["runs"] == [{"marker": marker}]
        return original(tool, arguments)

    monkeypatch.setattr(backend, "_execute_file", execute)
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(backend.execute, tmp_path, "read_file", {"reads": [{"path": p}]})
            for p in ("a.txt", "b.txt")
        ]
        results = [future.result(timeout=3) for future in futures]
    assert all(result.success for result in results)
    assert "alpha" in json.dumps(results[0].data) and "beta" in json.dumps(results[1].data)
    metrics = backend.last_tool_metrics
    assert metrics["runs"] == [{"marker": metrics["workspace_check_ms"]}]
    assert backend._active_metrics() is None


def test_native_process_call_excludes_file_reads(tmp_path, monkeypatch):
    backend = native(tmp_path)
    process_entered, release, read_entered = Event(), Event(), Event()

    def execute(workspace, name, arguments):
        if name == "run_command":
            process_entered.set()
            assert release.wait(3)
        else:
            read_entered.set()
        return ToolResult(True)

    monkeypatch.setattr(backend, "_execute", execute)
    with ThreadPoolExecutor(2) as pool:
        command = pool.submit(backend.execute, tmp_path, "run_command", {})
        assert process_entered.wait(3)
        read = pool.submit(backend.execute, tmp_path, "read_file", {})
        try:
            assert not read_entered.wait(0.05)
        finally:
            release.set()
        assert command.result(timeout=3).success and read.result(timeout=3).success


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Native scanner platforms")
def test_scanner_diagnostics_belong_to_calling_thread(tmp_path, monkeypatch):
    def scan(config):
        return {
            "diagnostics": {"workspace": config["workspace"]},
            "metrics": {"complete": True},
            "masks": [],
            "git_paths": [],
            "error": None,
        }

    monkeypatch.setitem(sys.modules, "rust_backend", SimpleNamespace(API_VERSION=1, scan=scan))
    scanner = RustPolicyScanner()
    both_returned = Barrier(2)

    def run(name):
        root = tmp_path / name
        root.mkdir()
        plan = PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES)
        scanner.scan(plan, ScanRequest((), False, ""))
        both_returned.wait(3)
        return scanner.last_diagnostics["workspace"]

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(run, name) for name in ("a", "b")]
        assert [f.result(timeout=3) for f in futures] == [
            bytes(tmp_path / name) for name in ("a", "b")
        ]
    assert scanner.last_diagnostics is None


def test_cancelled_gate_waiter_never_enters_and_releases_waiter_state():
    gate = BackendGate()
    context = CancellationContext()
    entered = Event()

    def waiting_writer():
        with cancellation_scope(context), gate.hold():
            entered.set()

    with ThreadPoolExecutor(1) as pool:
        with gate.hold(read=True):
            future = pool.submit(waiting_writer)
            context.cancel()
            with pytest.raises(RunCancelled):
                future.result(timeout=3)
        with gate.hold(read=True):
            assert not entered.is_set()


def test_docker_failure_is_sticky_and_waiting_call_cannot_start(tmp_path, monkeypatch):
    backend = object.__new__(DockerBackend)
    backend.healthy = True
    backend.policy = SandboxPolicy()
    backend.executable = "docker"
    backend.image = "sha256:test"
    entered, release = Event(), Event()
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(3)
        return ProcessResult(0, '{"success":true}', "", False, None, 1, False, False)

    backend.runner = SimpleNamespace(run=run)
    monkeypatch.setattr(
        "sandbox.docker.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b"cleanup refused"),
    )
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(backend.execute, tmp_path, "read_file", {})
        assert entered.wait(3)
        second = pool.submit(backend.execute, tmp_path, "read_file", {})
        release.set()
        with pytest.raises(OSError, match="清理失败"):
            first.result(timeout=3)
        with pytest.raises(OSError, match="之前"):
            second.result(timeout=3)
    assert not backend.healthy and len(calls) == 1


def test_docker_guard_updates_are_inside_session_serialization(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def execute(workspace, name, arguments):
        calls.append(name)
        return ToolResult(True)

    def record(*_):
        entered.set()
        assert release.wait(3)

    session = SimpleNamespace(
        workspace=tmp_path,
        backend=SimpleNamespace(execute=execute),
        guard=SimpleNamespace(record=record),
    )
    tool = native(tmp_path).tools()[0]
    proxy = SandboxedTool(tool.definition, session, execution_kind=ExecutionKind.SANDBOXED_PROCESS)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(proxy.execute, {})
        assert entered.wait(3)
        second = pool.submit(proxy.execute, {})
        try:
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=3).success and second.result(timeout=3).success
