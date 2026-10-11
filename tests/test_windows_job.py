"""Trusted client Job contracts plus Windows kernel tests (no Docker needed)."""

import io
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled
from host_support.processes import ProcessRunner, ProcessStartError
from host_support.windows_isolation import HANDLE_LIST, JOB_LIST, WindowsIsolationAPI
from host_support.windows_job import WindowsJobProcess


class FakeAPI:
    def __init__(self, fail=None):
        self.handles = set()
        self.next_handle = 0
        self.events = []
        self.fail = fail
        self.running = False

    def event(self, name):
        self.events.append(name)
        if self.fail == name:
            raise OSError("injected " + name)

    def handle(self, own):
        self.next_handle += 1
        self.handles.add(self.next_handle)
        return own(self.next_handle)

    def create_job(self, own):
        self.job = self.handle(own)
        self.event("job")
        return self.job

    def create_pipe(self, own, **kw):
        self.event("pipe")
        return self.handle(own), self.handle(own)

    def create_trusted_suspended(self, job, stdio, command, cwd, environment, own):
        self.event("create")
        assert job == self.job
        assert set(stdio) <= self.handles
        assert environment == "SystemRoot=C:\\Windows\x00\x00"
        self.running = True
        return SimpleNamespace(process=self.handle(own), thread=self.handle(own), pid=123)

    def resume(self, handle):
        self.event("resume")

    def terminate_job(self, job):
        self.event("terminate")
        self.running = False

    def active_processes(self, job):
        self.event("query")
        return int(self.running)

    def poll(self, process):
        return None if self.running else 1

    def close_handle(self, handle):
        self.event("close_job" if handle == self.job else "close")
        self.handles.remove(handle)


@pytest.fixture
def fake_streams(monkeypatch):
    streams = []

    def stream(handle, release):
        release(handle)
        value = io.BytesIO()
        streams.append((handle, value))
        return value

    monkeypatch.setattr("host_support.windows_job._pipe_stream", stream)
    return streams


def launch(api):
    return WindowsJobProcess(
        ["C:\\trusted\\docker.exe"],
        cwd="C:\\workspace",
        environment={"SystemRoot": "C:\\Windows"},
        api=api,
    )


def test_trusted_launch_has_atomic_job_and_explicit_handles_only(monkeypatch):
    api = object.__new__(WindowsIsolationAPI)
    captured = {}

    def launch(attributes, *args, **kwargs):
        captured.update(attributes)
        return "suspended"

    monkeypatch.setattr(api, "_launch_suspended", launch)
    assert (
        api.create_trusted_suspended(7, (1, 2, 3), ["docker.exe"], "C:\\", "", None) == "suspended"
    )
    assert set(captured) == {JOB_LIST, HANDLE_LIST}
    assert list(captured[JOB_LIST]) == [7]
    assert list(captured[HANDLE_LIST]) == [1, 2, 3]


@pytest.mark.parametrize("fail", [None, "terminate", "query", "close_job"])
def test_job_cleanup_confirms_kernel_accounting_and_closes_job_last(fake_streams, fail):
    api = FakeAPI()
    process = launch(api)
    api.fail = fail
    error = process.cleanup(lambda: None)
    assert bool(error) == bool(fail)
    assert api.events[-1] == "close_job"
    if fail is None:
        assert (
            api.events.index("terminate")
            < api.events.index("query")
            < api.events.index("close_job")
        )
        assert process.poll() == 1
    assert api.handles == {h for h, _ in fake_streams} | (
        {api.job} if fail == "close_job" else set()
    )
    # Repeating cleanup preserves uncertainty; closing a Job alone is not proof.
    assert process.cleanup(lambda: None) == error


@pytest.mark.parametrize("fail", ["job", "pipe", "create", "resume"])
def test_startup_failure_releases_owned_resources_without_fallback(fake_streams, fail):
    api = FakeAPI(fail)
    with pytest.raises(OSError, match="injected"):
        launch(api)
    assert api.events[-1] == "close_job"
    assert api.handles == {h for h, _ in fake_streams}
    assert all(stream.closed for _, stream in fake_streams)
    assert api.events.count("create") <= 1
    if fail != "resume":
        assert "resume" not in api.events


def test_active_job_timeout_cannot_be_reported_as_confirmed(fake_streams, monkeypatch):
    from host_support import windows_job

    api = FakeAPI()
    process = launch(api)
    monkeypatch.setattr(api, "active_processes", lambda job: 1)
    ticks = iter([0, 3])
    monkeypatch.setattr(windows_job, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    assert process.cleanup(lambda: None)
    assert "active processes" in process.diagnostics[0]["error"]
    assert api.events[-1] == "close_job"


@pytest.mark.parametrize("outcome", ["delayed_exit", "never_exits", "poll_error"])
def test_empty_job_waits_for_leader_signal_without_assuming_success(
    fake_streams, monkeypatch, outcome
):
    from host_support import windows_job

    api = FakeAPI()
    process = launch(api)
    clock = SimpleNamespace(now=0.0)
    polls, drains = [], []

    def poll(handle):
        assert api.events[-1] != "close_job"
        assert not api.running  # Job already empty, but leader not signaled yet.
        polls.append(clock.now)
        if outcome == "poll_error":
            raise OSError("leader query failed")
        if outcome == "delayed_exit" and len(polls) == 3:
            return 1
        return None

    def sleep(seconds):
        clock.now += 0.5

    monkeypatch.setattr(api, "poll", poll)
    monkeypatch.setattr(
        windows_job, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )
    error = process.cleanup(lambda: drains.append(clock.now))
    assert bool(error) == (outcome != "delayed_exit")
    assert api.events[-1] == "close_job"
    assert clock.now <= 2
    if outcome == "delayed_exit":
        assert len(polls) == 3 and len(drains) == 2
        assert process.returncode == 1
    elif outcome == "never_exits":
        assert clock.now == 2
        assert "leader exit" in process.diagnostics[0]["error"]
    else:
        assert "leader query failed" in process.diagnostics[0]["error"]
    assert process.cleanup(lambda: pytest.fail("already closed")) == error


def test_job_and_leader_wait_share_one_deadline(fake_streams, monkeypatch):
    from host_support import windows_job

    api = FakeAPI()
    process = launch(api)
    clock = SimpleNamespace(now=0.0)
    polls = []

    def sleep(seconds):
        clock.now += 0.5

    def poll(handle):
        polls.append(clock.now)
        # Would succeed within a fresh 2 s leader timeout, but not the single
        # total cleanup budget after waiting 1.5 s for the Job to become empty.
        return 1 if clock.now >= 2.5 else None

    monkeypatch.setattr(api, "active_processes", lambda job: int(clock.now < 1.5))
    monkeypatch.setattr(api, "poll", poll)
    monkeypatch.setattr(
        windows_job, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )
    assert process.cleanup(lambda: None)
    assert polls == [1.5, 2.0]
    assert clock.now == 2 and api.events[-1] == "close_job"


def test_runner_does_not_fallback_after_windows_job_start_failure(tmp_path, monkeypatch):
    from host_support import processes

    runner = ProcessRunner(supervise_tree=True)
    # Do not mutate global os.name: pathlib and pytest also depend on it.
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))

    def fail(*a, **k):
        raise OSError("Job creation failed")

    monkeypatch.setattr("host_support.windows_job.WindowsJobProcess", fail)
    monkeypatch.setattr(processes, "start_process", lambda *a, **k: pytest.fail("unsafe fallback"))
    context = CancellationContext()
    with pytest.raises(ProcessStartError):
        runner.run(["docker.exe"], cwd=tmp_path, cancellation=context)
    assert runner.last_cleanup_status == context.report()["cleanup_status"] == "unknown"


@pytest.mark.parametrize("mode", ["completed", "timeout", "cancel"])
@pytest.mark.parametrize("fail", [None, "query"])
def test_runner_propagates_windows_job_cleanup(tmp_path, monkeypatch, fake_streams, mode, fail):
    from host_support import processes

    api = FakeAPI()
    context = CancellationContext()
    leader_polls = []

    def delayed_leader(handle):
        leader_polls.append(handle)
        return 1 if len(leader_polls) >= 3 else None

    monkeypatch.setattr(api, "poll", delayed_leader)
    runner = ProcessRunner(supervise_tree=True)
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr("host_support.windows_job.WindowsJobProcess", lambda *a, **k: launch(api))
    calls = []

    def collect(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            return True
        api.fail = fail
        if mode == "cancel":
            context.cancel()
            context.check()
        return mode == "completed"

    monkeypatch.setattr(runner, "_collect_output", collect)
    monkeypatch.setattr(ProcessRunner, "_read_available", staticmethod(lambda stream: b""))
    expected = "unknown" if fail else "confirmed"
    if mode == "cancel":
        with pytest.raises(RunCancelled) as error:
            runner.run(["docker.exe"], cwd=tmp_path, cancellation=context)
        assert error.value.report["cleanup_status"] == expected
    else:
        result = runner.run(["docker.exe"], cwd=tmp_path, cancellation=context)
        assert result.cleanup_status == expected
        assert bool(result.cleanup_error) == bool(fail)
        assert result.timed_out == (mode == "timeout")
    assert runner.last_cleanup_status == expected
    assert context.report()["cleanup_status"] == expected
    assert api.events[-1] == "close_job"
    assert all(stream.closed for _, stream in fake_streams)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows kernel Job Objects")
@pytest.mark.parametrize("mode", ["completed", "timeout", "cancel"])
def test_real_windows_job_cleans_detached_descendants(tmp_path, mode):
    """Leader exit, timeout, repeated cancel: detached child must stop writing."""
    child = tmp_path / "child.py"
    child.write_text(
        "from pathlib import Path\nimport time\n"
        "while True:\n"
        " with open('heartbeat', 'ab') as f: f.write(b'x')\n"
        " Path('ready').touch()\n time.sleep(.02)\n"
    )
    parent = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "subprocess.Popen([sys.executable, 'child.py'], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=subprocess.DETACHED_PROCESS)\n"
        "deadline=time.monotonic()+10\n"
        "while not Path('ready').exists():\n"
        " assert time.monotonic()<deadline\n time.sleep(.01)\n"
        "print('leader ready', flush=True)\n" + ("" if mode == "completed" else "time.sleep(60)\n")
    )
    context = CancellationContext()
    stop = threading.Event()

    def cancel_when_ready():
        while not stop.wait(0.01):
            if (tmp_path / "ready").exists():
                context.cancel()
                context.cancel()
                return

    thread = threading.Thread(target=cancel_when_ready, daemon=True)
    if mode == "cancel":
        thread.start()
    runner = ProcessRunner(supervise_tree=True)
    try:
        if mode == "cancel":
            with pytest.raises(RunCancelled) as error:
                runner.run(
                    [sys.executable, "-c", parent],
                    cwd=tmp_path,
                    timeout_seconds=15,
                    cancellation=context,
                )
            assert error.value.report["cleanup_status"] == "confirmed", error.value.report
        else:
            result = runner.run([sys.executable, "-c", parent], cwd=tmp_path, timeout_seconds=5)
            assert result.cleanup_status == "confirmed", result
            assert result.cleanup_error is None
            assert result.timed_out == (mode == "timeout")
            assert "leader ready" in result.stdout
            if mode == "completed":
                assert result.exit_code == 0
        assert runner.last_cleanup_status == "confirmed"
        assert (tmp_path / "ready").exists()
        heartbeat = (tmp_path / "heartbeat").read_bytes()
        time.sleep(0.2)
        assert (tmp_path / "heartbeat").read_bytes() == heartbeat
        # A fresh command must work; cancellation never poisons the runner.
        resumed = runner.run([sys.executable, "-c", "print('resumed')"], cwd=tmp_path)
        assert resumed.exit_code == 0 and resumed.cleanup_status == "confirmed"
        assert resumed.stdout.strip() == "resumed"
    finally:
        stop.set()
        if thread.ident:
            thread.join(timeout=1)
