"""Linux /proc and pidfd behaviour, testable without a Linux host."""

import errno
import signal
from types import SimpleNamespace

import pytest

from tools._internal import process_supervisor as module
from tools._internal.process_supervisor import ProcessIdentity, ProcessSupervisor, ProcessTable


@pytest.fixture
def linux_table(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    table = ProcessTable()
    table.proc_root = tmp_path
    return table


def write_stat(table, pid, *, name=b"worker", state=b"S", start=b"9876"):
    path = table.proc_root / str(pid)
    path.mkdir(exist_ok=True)
    # Fields 3..22: state, ppid, pgrp, ... starttime.
    fields = [state, b"10", b"11", *([b"0"] * 16), start]
    (path / "stat").write_bytes(str(pid).encode() + b" (" + name + b") " + b" ".join(fields))


def test_proc_accepts_arbitrary_name_bytes_and_ignores_non_pid_entries(linux_table):
    write_stat(linux_table, 12, name=b"a ) \xff\n(b")
    (linux_table.proc_root / "self").symlink_to("12")
    assert linux_table.snapshot() == {12: ProcessIdentity(12, 10, 11, (9876, 0))}


@pytest.mark.parametrize("state", [b"Z", b"X", b"x"])
def test_proc_recognizes_zombies_and_dead_tasks(linux_table, state):
    write_stat(linux_table, 12, state=state)
    assert linux_table.read(12).zombie


def test_disappeared_process_is_skipped(linux_table):
    (linux_table.proc_root / "12").mkdir()
    assert linux_table.read(12) is None
    assert linux_table.snapshot() == {}


@pytest.mark.parametrize("content", [b"12 (truncated) S 10", b"bad", b"wrong pid"])
def test_incomplete_proc_identity_is_an_error(linux_table, content):
    write_stat(linux_table, 12)
    (linux_table.proc_root / "12/stat").write_bytes(content)
    with pytest.raises(OSError, match="Incomplete"):
        linux_table.snapshot()


def test_non_numeric_start_time_is_an_error(linux_table):
    write_stat(linux_table, 12, start=b"bad")
    with pytest.raises(OSError, match="Invalid"):
        linux_table.read(12)


@pytest.fixture
def pidfd_supervisor(monkeypatch):
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    identity = ProcessIdentity(101, 1, 101, (1, 0))
    table = SimpleNamespace(read=lambda pid: identity)
    monkeypatch.setattr(module, "ProcessTable", lambda: table)
    supervisor = ProcessSupervisor(SimpleNamespace(pid=101))
    events = []
    monkeypatch.setattr(
        module.os,
        "pidfd_open",
        lambda pid, flags: events.append(("open", pid)) or 42,
        raising=False,
    )
    monkeypatch.setattr(
        module.signal,
        "pidfd_send_signal",
        lambda fd, sig: events.append(("send", fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(module.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(module.os, "kill", lambda pid, sig: events.append(("kill", pid, sig)))
    return supervisor, identity, table, events


def test_pidfd_is_used_and_closed(pidfd_supervisor):
    supervisor, identity, _, events = pidfd_supervisor
    supervisor._signal(identity, signal.SIGTERM)
    assert events == [("open", 101), ("send", 42, signal.SIGTERM), ("close", 42)]
    assert supervisor.diagnostics[-1]["via"] == "pidfd"


def test_identity_is_rechecked_after_binding_pidfd(pidfd_supervisor, monkeypatch):
    supervisor, identity, table, events = pidfd_supervisor

    def open_reused(pid, flags):
        table.read = lambda pid: ProcessIdentity(101, 1, 101, (2, 0))
        return 42

    monkeypatch.setattr(module.os, "pidfd_open", open_reused)
    supervisor._signal(identity, signal.SIGKILL)
    assert events == [("close", 42)]


def test_old_kernel_falls_back_with_identity_check(pidfd_supervisor, monkeypatch):
    supervisor, identity, _, events = pidfd_supervisor

    def unavailable(*args):
        raise OSError(errno.ENOSYS, "old kernel")

    monkeypatch.setattr(module.os, "pidfd_open", unavailable)
    supervisor._signal(identity, signal.SIGTERM)
    assert events == [("kill", 101, signal.SIGTERM)]
    assert supervisor.diagnostics[0]["stage"] == "pidfd_unavailable"
    assert supervisor.diagnostics[-1]["via"] == "pid"


@pytest.mark.parametrize("missing", ["pidfd_open", "pidfd_send_signal"])
def test_missing_python_api_uses_portable_signalling(pidfd_supervisor, monkeypatch, missing):
    supervisor, identity, _, events = pidfd_supervisor
    target = module.os if missing == "pidfd_open" else module.signal
    monkeypatch.delattr(target, missing)
    supervisor._signal(identity, signal.SIGTERM)
    assert events == [("kill", 101, signal.SIGTERM)]
    assert supervisor.diagnostics[-1]["via"] == "pid"


def test_identity_inspection_failure_closes_pidfd(pidfd_supervisor):
    supervisor, identity, table, events = pidfd_supervisor

    def denied(pid):
        raise PermissionError(errno.EACCES, "identity unreadable")

    table.read = denied
    supervisor._signal(identity, signal.SIGTERM)
    assert events == [("open", 101), ("close", 42)]
    assert supervisor.diagnostics[-1]["stage"] == "identity"
    assert supervisor.diagnostics[-1]["errno"] == errno.EACCES


@pytest.mark.parametrize("stage", ["open", "send"])
def test_permission_denial_never_falls_back_to_pid_kill(pidfd_supervisor, monkeypatch, stage):
    supervisor, identity, _, events = pidfd_supervisor

    def denied(*args):
        raise PermissionError(errno.EPERM, "denied")

    if stage == "open":
        monkeypatch.setattr(module.os, "pidfd_open", denied)
    else:
        monkeypatch.setattr(module.signal, "pidfd_send_signal", denied)
    supervisor._signal(identity, signal.SIGTERM)
    assert all(event[0] != "kill" for event in events)
    assert supervisor.diagnostics[-1]["errno"] == errno.EPERM
    assert supervisor.diagnostics[-1]["stage"] == (
        "pidfd_open" if stage == "open" else "pidfd_send_signal"
    )
    if stage == "send":
        assert events[-1] == ("close", 42)


def test_exited_process_closes_pidfd_without_error(pidfd_supervisor, monkeypatch):
    supervisor, identity, _, events = pidfd_supervisor

    def gone(*args):
        raise ProcessLookupError(errno.ESRCH, "gone")

    monkeypatch.setattr(module.signal, "pidfd_send_signal", gone)
    supervisor._signal(identity, signal.SIGKILL)
    assert events == [("open", 101), ("close", 42)]
    assert supervisor.diagnostics == []
