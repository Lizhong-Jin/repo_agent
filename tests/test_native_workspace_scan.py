"""Platform-independent checks for native preflight traversal and fresh metadata."""

import errno
import os
import stat
from collections import Counter
from pathlib import Path

import pytest

from sandbox.linux_native import LinuxNativeBackend
from sandbox.native import NativeBackend


def backend_for(root, cls=LinuxNativeBackend):
    backend = object.__new__(cls)
    backend.workspace = root
    backend.read_paths = ()
    return backend


def test_linux_checks_each_entry_once_and_does_not_skip_dependency_trees(tmp_path, monkeypatch):
    files = []
    for directory in ("src", ".venv/lib", "logs"):
        parent = tmp_path / directory
        parent.mkdir(parents=True)
        for i in range(5):
            path = parent / str(i)
            path.write_text("content")
            files.append(path)
    calls, visits = Counter(), Counter()
    original_lstat, original_scandir = os.lstat, os.scandir

    def lstat(path, *args, **kwargs):
        calls[Path(path)] += 1
        return original_lstat(path, *args, **kwargs)

    def scandir(path):
        visits[Path(path)] += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os, "scandir", scandir)
    backend_for(tmp_path)._check_workspace()
    assert all(calls[path] == 1 for path in files)
    assert len(visits) == 5 and set(visits.values()) == {1}


@pytest.mark.parametrize("cls", [NativeBackend, LinuxNativeBackend])
@pytest.mark.parametrize("directory", ["src", ".venv", "logs"])
def test_link_created_outside_workspace_after_scan_is_detected(tmp_path, cls, directory):
    root = tmp_path / "project"
    parent = root / directory
    parent.mkdir(parents=True)
    target = parent / "file"
    target.write_text("content")
    backend = backend_for(root, cls)
    backend._check_workspace()
    (tmp_path / "outside-link").hardlink_to(target)
    with pytest.raises(ValueError, match="硬链接"):
        backend._check_workspace()


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_linux_rejects_special_files(tmp_path, monkeypatch, kind):
    path = tmp_path / "special"
    if kind == "fifo":
        os.mkfifo(path)
        with pytest.raises(ValueError, match="特殊文件"):
            backend_for(tmp_path)._check_workspace()
    else:
        # Some test hosts forbid AF_UNIX bind. Exercise socket metadata without
        # requiring host IPC permissions; FIFO above uses a real special file.
        path.touch()
        original = os.lstat

        def socket_metadata(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if Path(candidate) == path:
                return os.stat_result((stat.S_IFSOCK | 0o600, *tuple(info)[1:]))
            return info

        monkeypatch.setattr(os, "lstat", socket_metadata)
        with pytest.raises(ValueError, match="特殊文件"):
            backend_for(tmp_path)._check_workspace()


def test_symlink_directories_are_not_followed(tmp_path):
    root, outside = tmp_path / "project", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.mkfifo(outside / "special")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    (root / "dangling").symlink_to(root / "missing")
    backend_for(root)._check_workspace()


def test_metadata_error_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("content")
    original = os.lstat

    def fail(path, *args, **kwargs):
        if Path(path) == target:
            raise PermissionError(errno.EACCES, "denied", str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", fail)
    with pytest.raises(PermissionError):
        backend_for(tmp_path)._check_workspace()
