"""Run these contracts on real Windows, Linux and macOS kernels."""

import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from host_support import filesystem as fs
from host_support.locking import lock_descriptor
from host_support.storage import atomic_write
from tools._internal.file_access import FileAccess


def test_relative_io_stage_move_delete_and_prunable_walk(tmp_path):
    with FileAccess(tmp_path).activate() as access:
        access.mkdir(tmp_path / "nested/深层", parents=True)
        staged = access.stage(tmp_path / "nested/深层/a.txt", b"hello\r\n", 0o644)
        try:
            staged.replace(tmp_path / "nested/深层/a.txt")
        finally:
            staged.unlink(missing_ok=True)
        with access.open_read(tmp_path / "nested/深层/a.txt") as stream:
            assert stream.read() == b"hello\r\n"
        access.rename(tmp_path / "nested/深层/a.txt", tmp_path / "renamed.txt")
        assert set(access.iterdir(tmp_path)) == {tmp_path / "nested", tmp_path / "renamed.txt"}
        iterator = fs.walk_descriptors(tmp_path)
        try:
            _, dirs, names, fd = next(iterator)
            assert names == ["renamed.txt"]
            assert fs.stat_at("renamed.txt", dir_fd=fd).st_size == 7
            dirs.clear()
            assert list(iterator) == []
        finally:
            iterator.close()
        access.unlink(tmp_path / "renamed.txt")
        assert not (tmp_path / "renamed.txt").exists()


def test_atomic_storage_failure_cleanup_and_binary_append(tmp_path):
    path = tmp_path / "state.json"
    atomic_write(path, b"original", sync=True, mode=0o600)

    def fail():
        raise OSError("injected conflict")

    with pytest.raises(OSError, match="injected conflict"):
        atomic_write(path, b"replacement", sync=True, mode=0o600, before_replace=fail)
    assert path.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [path]
    atomic_write(path, b"new\r\n", sync=True, mode=0o600)
    fd = fs.open_file(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "wb") as stream:
        stream.write(b"tail\r\n")
    assert path.read_bytes() == b"new\r\ntail\r\n"


@pytest.mark.parametrize("failure", ["sync", "replace"])
def test_atomic_storage_io_failure_preserves_original(tmp_path, monkeypatch, failure):
    from host_support import storage

    target = tmp_path / "state"
    atomic_write(target, b"before")

    def fail(*args, **kwargs):
        raise OSError("injected I/O error")

    if failure == "sync":
        monkeypatch.setattr(storage.os, "fsync", fail)
    else:
        monkeypatch.setattr(storage.os, "replace", fail)
        monkeypatch.setattr(storage, "rename_at", fail)
    with pytest.raises(OSError, match="injected I/O error"):
        atomic_write(target, b"after", sync=True)
    assert target.read_bytes() == b"before"
    assert list(tmp_path.iterdir()) == [target]


def test_lock_contention_across_processes_and_release(tmp_path):
    path = tmp_path / "lock"
    fd = fs.open_file(path, os.O_RDWR | os.O_CREAT)
    code = """
import os, sys
from host_support.filesystem import open_file
from host_support.locking import lock_descriptor
fd = open_file(sys.argv[1], os.O_RDWR)
try:
    lock_descriptor(fd, blocking=False)
except BlockingIOError:
    print('busy')
else:
    print('acquired')
finally:
    os.close(fd)
"""
    def child():
        return subprocess.run([sys.executable, "-c", code, str(path)],
                              capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    try:
        lock_descriptor(fd, blocking=False)
        assert child() == "busy"
    finally:
        os.close(fd)
    assert child() == "acquired"
    assert path.read_bytes() == b""  # Locks must not insert bytes into empty files.


def test_blocking_lock_waits_for_close(tmp_path):
    path = tmp_path / "lock"
    fd = fs.open_file(path, os.O_RDWR | os.O_CREAT)
    lock_descriptor(fd)
    process = subprocess.Popen([sys.executable, "-u", "-c", """
import os, sys
from host_support.filesystem import open_file
from host_support.locking import lock_descriptor
fd = open_file(sys.argv[1], os.O_RDWR)
print('ready', flush=True)
lock_descriptor(fd)
print('acquired', flush=True)
os.close(fd)
""", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        assert process.poll() is None
        os.close(fd)
        fd = None
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "acquired"
    finally:
        if fd is not None:
            os.close(fd)
        if process.poll() is None:
            process.kill()
        process.communicate()


def test_hardlink_content_is_never_read_or_truncated_by_file_service(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"keep")
    os.link(original, tmp_path / "alias")
    with FileAccess(tmp_path).activate() as access:
        with pytest.raises(PermissionError):
            with access.open_read(tmp_path / "alias"):
                pytest.fail("Hard link was opened")
    assert original.read_bytes() == b"keep"


def test_session_log_history_and_diagnostics_share_host_storage(tmp_path):
    from agent.compaction_diagnostics import save_diagnostic
    from agent.history import HistoryArchive
    from agent.session import SessionStore, open_log
    from llm import Message

    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(project, directory=tmp_path / "state").open()
    try:
        with pytest.raises(ValueError, match="已有"):
            SessionStore(project, directory=tmp_path / "state").open()
        record = dict(history=[], model=dict(provider="test", model="test", endpoint="test"),
                      status={}, transcript=[], mode="local", sandbox_healthy=True,
                      pending_task=None, task_number=0)
        store.save(record)
        with open_log(store.directory / "events.log", write=True) as stream:
            stream.write("一\n")
        with open_log(store.directory / "events.log", write=True) as stream:
            stream.write("二\n")
        with open_log(store.directory / "events.log") as stream:
            assert stream.read() == "一\n二\n"
        archive = HistoryArchive(store)
        archive.archive([Message(role="user", content="hello")])
        with archive.connect() as db:
            assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        diagnostic = save_diagnostic(store, {"reason": "test"})
        saved = store.directory / store.id / "compaction-diagnostics" / (diagnostic + ".json")
        assert json.loads(saved.read_text(encoding="utf-8"))["reason"] == "test"
    finally:
        store.close()
    restored = SessionStore(project, directory=tmp_path / "state").open()
    try:
        assert restored.id == store.id and restored.data["history"] == []
    finally:
        restored.close()


def test_docker_snapshot_writeback_conflict_resume_and_restore(tmp_path):
    from sandbox.session import SandboxSession

    project = tmp_path / "工作区 with spaces"
    project.mkdir()
    (project / "a.txt").write_bytes(b"before\r\n")
    (project / "remove.txt").write_bytes(b"delete me")
    (project / ".env").write_bytes(b"secret")
    session = SandboxSession(project, backend=SimpleNamespace(healthy=True))
    try:
        assert session.changes()[1] == []
        assert not (session.workspace / ".env").exists()
        (session.workspace / "a.txt").write_bytes(b"after\n")
        (session.workspace / "remove.txt").unlink()
        (session.workspace / "nested").mkdir()
        (session.workspace / "nested/new.txt").write_bytes(b"new")
        (project / "a.txt").write_bytes(b"human")
        with pytest.raises(ValueError, match="原项目"):
            session.apply()
        assert not (project / "nested").exists()
        (project / "a.txt").write_bytes(b"before\r\n")
        session = SandboxSession.review(session.directory)
        assert session.apply() == ["a.txt", "nested/new.txt", "remove.txt"]
        backup_id = session.last_backup.name
        assert session.changes()[1] == []
        assert (project / "a.txt").read_bytes() == b"after\n"
        assert session.restore(backup_id) == ["a.txt", "nested/new.txt", "remove.txt"]
        assert (project / "a.txt").read_bytes() == b"before\r\n"
        assert (project / "remove.txt").read_bytes() == b"delete me"
        assert not (project / "nested/new.txt").exists()
    finally:
        shutil.rmtree(session.directory)


def test_partial_writeback_failure_can_be_restored(tmp_path, monkeypatch):
    from sandbox import session as module

    (tmp_path / "a").write_bytes(b"before")
    session = module.SandboxSession(tmp_path, backend=SimpleNamespace(healthy=True))
    try:
        (session.workspace / "a").write_bytes(b"after")
        (session.workspace / "b").write_bytes(b"new")
        original = module.rename_at

        def fail_second(source, destination, **kwargs):
            if destination == "b":
                raise OSError("injected writeback failure")
            return original(source, destination, **kwargs)

        monkeypatch.setattr(module, "rename_at", fail_second)
        with pytest.raises(OSError, match="injected writeback failure"):
            session.apply()
        assert (tmp_path / "a").read_bytes() == b"after"
        assert not (tmp_path / "b").exists()
        backup = session.last_backup.name
        monkeypatch.setattr(module, "rename_at", original)
        assert session.restore(backup) == ["a"]
        assert (tmp_path / "a").read_bytes() == b"before"
        assert not list(tmp_path.glob(".sandbox-*"))
    finally:
        shutil.rmtree(session.directory)
