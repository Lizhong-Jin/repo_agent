"""The Windows running-cancellation test must wait through preparation and resume."""

import shutil
from types import SimpleNamespace

import pytest
import test_permission_kernel as kernel

from host_support.cancellation import checkpoint
from host_support.windows_isolation import WindowsIsolationAPI
from sandbox import permission_inputs


def test_windows_watchdog_starts_only_after_resume(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    staged = profile / "inputs" / "input-id"
    staged.mkdir(parents=True)
    workspace = profile / "workspace"
    workspace.mkdir()
    events = []
    threads = []

    class Thread:
        ident = None

        def __init__(self, target):
            self.target = target
            threads.append(self)

        def start(self):
            self.ident = 1
            events.append("watch")
            self.target()

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

    def resume(api, thread):
        events.append("resume")
        (workspace / "started.txt").write_text("running")

    def execute(*args):
        permission_inputs.stage_permission_inputs(None, None)
        # Even arbitrarily slow ACL preparation must not start the launch timer.
        assert threads[0].ident is None
        events.append("sealed")
        WindowsIsolationAPI.resume(None, None)
        try:
            checkpoint()
            pytest.fail("The marker must trigger cancellation")
        finally:
            shutil.rmtree(staged)

    monkeypatch.setattr(kernel, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        kernel, "threading", SimpleNamespace(Thread=Thread, Event=kernel.threading.Event)
    )
    monkeypatch.setattr(WindowsIsolationAPI, "resume", resume)
    monkeypatch.setattr(
        permission_inputs, "stage_permission_inputs", lambda *a: ({"input-id": str(staged)}, [])
    )
    backend = SimpleNamespace(workspace=workspace, execute=execute, healthy=True)
    kernel.test_native_running_cancellation_removes_inputs_after_process_cleanup(
        (backend, None, None, None), monkeypatch
    )
    assert events == ["sealed", "resume", "watch"]
