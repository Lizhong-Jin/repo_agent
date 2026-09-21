import errno
import os
import signal
import sys
from types import SimpleNamespace

import pytest

from tools.process_runner import ProcessRunner
from tools.process_supervisor import ProcessIdentity, ProcessSupervisor


def fake_supervisor(monkeypatch, snapshot):
    # These shared ownership tests exercise the portable PID signalling path.
    # Linux pidfd behaviour is covered separately with controlled descriptors.
    monkeypatch.setattr("tools.process_supervisor.sys", SimpleNamespace(platform="darwin"))
    table = SimpleNamespace(read=lambda pid: snapshot.get(pid), snapshot=lambda: dict(snapshot))
    monkeypatch.setattr("tools.process_supervisor.ProcessTable", lambda: table)
    process = SimpleNamespace(pid=101, poll=lambda: 0, wait=lambda **kw: 0)
    return ProcessSupervisor(process)


def test_tracks_observed_child_after_session_escape_and_reparenting(monkeypatch):
    snapshot = {
        101: ProcessIdentity(101, 1, 101, (1, 0)),
        102: ProcessIdentity(102, 101, 102, (2, 0)),
        999: ProcessIdentity(999, 1, 999, (1, 0)),
    }
    supervisor = fake_supervisor(monkeypatch, snapshot)
    assert {p.pid for p in supervisor.refresh(force=True)} == {101, 102}
    del snapshot[101]
    snapshot[102] = ProcessIdentity(102, 1, 102, (2, 0))
    signalled = []

    def kill(pid, sig):
        signalled.append(pid)
        del snapshot[pid]

    monkeypatch.setattr("tools.process_supervisor.os.kill", kill)
    assert supervisor.cleanup() is None
    assert signalled == [102]
    assert 999 in snapshot


def test_pid_reuse_is_checked_again_immediately_before_signal(monkeypatch):
    old = ProcessIdentity(101, 1, 101, (1, 0))
    snapshot = {101: old}
    supervisor = fake_supervisor(monkeypatch, snapshot)
    snapshot[101] = ProcessIdentity(101, 1, 101, (9, 0))
    monkeypatch.setattr(
        "tools.process_supervisor.os.kill", lambda *a: pytest.fail("Reused PID signalled")
    )
    supervisor._signal(old, signal.SIGTERM)
    assert supervisor.refresh(force=True) == []


def test_zombie_group_does_not_require_signal_permission(monkeypatch):
    snapshot = {101: ProcessIdentity(101, 1, 101, (1, 0), zombie=True)}
    supervisor = fake_supervisor(monkeypatch, snapshot)
    monkeypatch.setattr(
        "tools.process_supervisor.os.kill", lambda *a: pytest.fail("Zombie signalled")
    )
    assert supervisor.cleanup() is None
    assert supervisor.diagnostics[-1]["outcome"] == "confirmed"


def test_permission_failure_includes_stage_signal_and_errno(monkeypatch):
    snapshot = {101: ProcessIdentity(101, 1, 101, (1, 0))}
    supervisor = fake_supervisor(monkeypatch, snapshot)

    def denied(*args):
        raise PermissionError(errno.EPERM, "simulated restriction")

    monkeypatch.setattr("tools.process_supervisor.os.kill", denied)
    assert supervisor.cleanup() is not None
    assert any(
        d["errno"] == errno.EPERM and d["signal"] == signal.SIGKILL for d in supervisor.diagnostics
    )
    assert supervisor.diagnostics[-1]["outcome"] == "unknown"


def test_snapshot_failure_is_not_mistaken_for_empty_process_tree(monkeypatch):
    snapshot = {101: ProcessIdentity(101, 1, 101, (1, 0))}
    supervisor = fake_supervisor(monkeypatch, snapshot)

    def denied():
        raise PermissionError(errno.EPERM, "simulated restriction")

    supervisor.table.snapshot = denied
    assert supervisor.cleanup() is not None
    assert supervisor.diagnostics[0]["stage"] == "process_snapshot"


@pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="Host process metadata requires macOS or Linux",
)
def test_supervised_cancellation_reaps_child_before_propagating(tmp_path, monkeypatch):
    runner = ProcessRunner(supervise_tree=True)
    processes = []

    def interrupt(process, *args):
        processes.append(process)
        raise KeyboardInterrupt

    monkeypatch.setattr(runner, "_collect_output", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path)
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
    assert runner.last_cleanup_status == "confirmed"


@pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="Host process metadata requires macOS or Linux",
)
def test_supervised_timeout_retains_output_and_kills_detached_child(tmp_path):
    # Child is observable for long enough to be adopted before changing parent.
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('child ready', flush=True); time.sleep(20)"
    code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-u','-c',{child!r}], start_new_session=True); "
        "print('child_pid='+str(p.pid), flush=True); time.sleep(20)"
    )
    result = None
    try:
        result = ProcessRunner(supervise_tree=True).run(
            [sys.executable, "-u", "-c", code],
            cwd=tmp_path,
            timeout_seconds=1,
        )
        assert result.timed_out and result.status == "timed_out"
        assert "child ready" in result.stdout
        assert not result.output_complete
        assert result.cleanup_status == "confirmed", result
        assert result.cleanup_error is None
        assert any(d["signal"] == signal.SIGKILL for d in result.cleanup_diagnostics)
        assert result.duration_ms < 4500
    finally:
        # Safety net for a broken supervisor; only our child's reported PID.
        if result and "child_pid=" in result.stdout and result.cleanup_error:
            pid = int(result.stdout.split("child_pid=", 1)[1].splitlines()[0])
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
