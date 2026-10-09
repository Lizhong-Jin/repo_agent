"""A non-POSIX executor can replace native launch mechanics without bypassing policy."""

import json
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.processes import ProcessResult, ProcessRunner
from sandbox.native_common import NativeBackendBase
from sandbox.native_execution import NativeCleanupError
from sandbox.project_python import ProjectPython


class RecordingAdapter:
    process_cleanup = "test_owned_processes"

    def __init__(self):
        self.calls = []
        self.events = []
        self.prepare_error = None
        self.run_error = None
        self.release_error = None
        self.cleanup_status = "confirmed"
        self.selection = None

    def select_project_python(self, backend, explicit):
        self.selection = explicit
        # A test-only alternative layout, deliberately not bin/python.
        return ProjectPython(Path(explicit) if explicit else backend.python, "test selection", ())

    @contextmanager
    def prepare(self, backend, call):
        self.calls.append(call)
        self.events.append("prepare")
        with tempfile.TemporaryDirectory(dir=backend.directory) as path:
            self.resource = Path(path)
            try:
                if self.prepare_error:
                    raise self.prepare_error
                yield RecordingProcess(self, call)
            finally:
                self.events.append("release")
                if self.release_error:
                    raise self.release_error


class RecordingProcess:
    def __init__(self, adapter, call):
        self.adapter = adapter
        self.call = call
        self.last_cleanup_status = "not_needed"

    def run(self, *, timeout_seconds):
        self.adapter.events.append("run")
        self.adapter.timeout = timeout_seconds
        self.last_cleanup_status = self.adapter.cleanup_status
        if self.adapter.run_error:
            raise self.adapter.run_error
        return ProcessResult(
            0,
            json.dumps({"version": "test", "executable": "test"}),
            "",
            False,
            None,
            1,
            False,
            False,
            cleanup_status=self.last_cleanup_status,
        )


class Backend(NativeBackendBase):
    execution_adapter_type = RecordingAdapter

    def _platform_setup(self):
        pass

    def _read_paths(self):
        return ()

    def _preflight(self):
        self._run(["test-preflight"])


@pytest.fixture
def backend(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The replacement must not call the POSIX runner or interpreter authorizer")

    monkeypatch.setattr(ProcessRunner, "run", forbidden)
    monkeypatch.setattr("sandbox.project_python.authorized_read_paths", forbidden)
    with_backend = Backend(tmp_path, profile="standard")
    with_backend.execution_adapter.events.clear()
    with_backend.execution_adapter.calls.clear()
    try:
        yield with_backend
    finally:
        with_backend.close()


@pytest.mark.parametrize("worker", [False, True])
def test_replacement_owns_command_and_worker_preparation(backend, worker):
    request = {"name": "git_status", "arguments": {}} if worker else None
    result = backend._run(
        None if worker else ["test.exe", "a b"],
        request=request,
        cwd=backend.workspace,
        project=True,
        git_read=worker,
        timeout=17,
        max_output_bytes=1234,
    )
    adapter = backend.execution_adapter
    call = adapter.calls[-1]
    assert call.request == request
    assert call.command == (None if worker else ("test.exe", "a b"))
    assert call.cwd == backend.workspace and call.project and call.git_read == worker
    assert call.max_output_bytes == 1234 and adapter.timeout == 17
    assert adapter.events == ["prepare", "run", "release"]
    assert not adapter.resource.exists() and not list(backend.directory.glob("call-*"))
    assert result.cleanup_status == "confirmed" and backend.healthy
    assert backend.last_run_metrics["complete"]
    assert backend.execution_context()["persistence"]["process_cleanup"] == adapter.process_cleanup


def test_alternative_interpreter_layout_is_authorized_and_probed_by_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(ProcessRunner, "run", lambda *a, **kw: pytest.fail("POSIX runner"))
    chosen = tmp_path / "environment/Scripts/python.exe"
    backend = Backend(tmp_path, profile="standard", project_python=chosen)
    try:
        adapter = backend.execution_adapter
        assert adapter.selection == chosen and backend.project_python.executable == chosen
        assert adapter.calls[0].command == ("test-preflight",)
        assert adapter.calls[1].command[0] == str(chosen)
        assert adapter.calls[1].project
        assert backend.project_python_info["version"] == "test"
    finally:
        backend.close()


def test_failed_preparation_never_runs_or_falls_back(backend):
    adapter = backend.execution_adapter
    adapter.prepare_error = ValueError("policy rejected")
    with pytest.raises(ValueError, match="policy rejected"):
        backend._run(["test.exe"])
    assert adapter.events == ["prepare", "release"]
    assert not adapter.resource.exists() and backend.healthy
    assert not backend.last_run_metrics["complete"]
    assert backend.last_run_metrics["process_ms"] == 0


@pytest.mark.parametrize("status", ["confirmed", "unknown"])
def test_cancellation_releases_resources_and_preserves_cleanup_status(backend, status):
    context = CancellationContext()
    cancelled = RunCancelled(context)
    adapter = backend.execution_adapter
    adapter.run_error = cancelled
    adapter.cleanup_status = status
    with cancellation_scope(context), pytest.raises(RunCancelled) as caught:
        backend._run(["test.exe"])
    assert caught.value is cancelled
    assert adapter.events == ["prepare", "run", "release"]
    assert not adapter.resource.exists()
    assert backend.healthy == (status == "confirmed")
    if status == "unknown":
        assert context.report()["cleanup_status"] == "unknown"


@pytest.mark.parametrize("during_prepare", [False, True])
def test_resource_release_failure_marks_backend_unhealthy(backend, during_prepare):
    adapter = backend.execution_adapter
    if during_prepare:
        adapter.prepare_error = ValueError("policy rejected")
    adapter.release_error = NativeCleanupError("authorization release failed")
    with pytest.raises(NativeCleanupError, match="authorization release failed"):
        backend._run(["test.exe"])
    assert not backend.healthy and backend.last_cleanup["cleanup_status"] == "unknown"
    assert not backend.last_run_metrics["complete"]
    assert ("run" in adapter.events) is not during_prepare


def test_unconfirmed_normal_exit_degrades_execution_but_keeps_file_reads(backend):
    backend.execution_adapter.cleanup_status = "unknown"
    result = backend._run(["test.exe"])
    assert result.exit_code == 0 and not backend.healthy
    denied = backend.execute(backend.workspace, "run_command", {"command": ["test.exe"]})
    assert denied.error_code == "NATIVE_UNHEALTHY"
    (backend.workspace / "a.txt").write_text("still readable")
    read = backend.execute(backend.workspace, "read_file", {"reads": [{"path": "a.txt"}]})
    assert read.success and read.data["execution_allowed"] is False


def test_process_failure_with_confirmed_cleanup_keeps_backend_usable(backend):
    adapter = backend.execution_adapter
    adapter.run_error = OSError("start failed")
    with pytest.raises(OSError, match="start failed"):
        backend._run(["test.exe"])
    assert backend.healthy and not adapter.resource.exists()


def test_base_backend_has_no_implicit_executor(tmp_path):
    class MissingAdapter(Backend):
        execution_adapter_type = None

    with pytest.raises(NotImplementedError, match="execution adapter"):
        MissingAdapter(tmp_path, profile="standard")


def test_timeout_with_unconfirmed_cleanup_marks_backend_unhealthy(backend, monkeypatch):
    original = RecordingProcess.run

    def run(self, **kwargs):
        return replace(original(self, **kwargs), timed_out=True, cleanup_status="not_needed")

    monkeypatch.setattr(RecordingProcess, "run", run)
    backend._run(["test.exe"])
    assert not backend.healthy


def test_release_failure_after_confirmed_cancellation_overrides_cleanup_report(backend):
    context = CancellationContext()
    context.record_cleanup("confirmed", source="test_process")
    adapter = backend.execution_adapter
    adapter.run_error = RunCancelled(context)
    adapter.release_error = NativeCleanupError("authorization release failed")
    with cancellation_scope(context), pytest.raises(NativeCleanupError):
        backend._run(["test.exe"])
    assert context.report()["cleanup_status"] == "unknown" and not backend.healthy


def test_posix_resource_failure_before_yield_is_not_mistaken_for_policy_rejection(
    tmp_path,
    monkeypatch,
):
    from sandbox.posix_execution import PosixNativeExecutionAdapter

    class PosixBackend(Backend):
        execution_adapter_type = PosixNativeExecutionAdapter

        def _preflight(self):
            pass

        def _sandbox_command(self, *args, **kwargs):
            raise ValueError("policy rejected")

    backend = PosixBackend(tmp_path, profile="standard", isolated_workspace=True)
    try:
        with monkeypatch.context() as patch:

            def cleanup(self):
                raise OSError("injected directory cleanup failure")

            patch.setattr(tempfile.TemporaryDirectory, "cleanup", cleanup)
            with pytest.raises(NativeCleanupError, match="directory cleanup"):
                backend._run(["test.exe"])
        assert not backend.healthy
        assert backend.last_run_metrics["process_ms"] == 0
    finally:
        backend.close()
