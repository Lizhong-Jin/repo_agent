"""Pinned traversal, fresh preflight facts, and explicit macOS backend selection."""

import os
from types import SimpleNamespace

import pytest

from host_support.cancellation import RunCancelled, cancellation_scope
from host_support.rust_filesystem import RustFilesystem, select_directory_backend
from sandbox.macos_native import MacOSNativeBackend
from tools._internal.file_access import FileAccess


@pytest.fixture
def native():
    extension = pytest.importorskip("rust_backend")
    if getattr(extension, "FILESYSTEM_API_VERSION", None) != 1:
        pytest.skip("Rebuild the filesystem extension")
    return RustFilesystem()


def test_directory_is_pinned_repeated_enumeration_and_reader_lifetime(tmp_path, native):
    root = tmp_path.resolve()
    (root / "inside").mkdir()
    (root / "inside/中文").touch()
    (root / "outside").mkdir()
    (root / "outside/secret").touch()
    with FileAccess(root, directory_backend=native).activate() as access:
        with access.read_directory(root / "inside") as reader:
            (root / "inside").rename(root / "moved")
            (root / "inside").symlink_to(root / "outside", target_is_directory=True)
            assert reader.names() == reader.names() == ["中文"]
            assert reader.stat("中文").st_size == 0
            with reader.open_read("中文") as stream:
                assert stream.read() == b""
        with pytest.raises(ValueError, match="closed"):
            reader.names()
        with pytest.raises(OSError):
            os.fstat(reader.fd)
        with pytest.raises(OSError), access.read_directory(root / "inside"):
            pytest.fail("Followed replacement link")


@pytest.mark.parametrize("part", [b"..", b".", b"a/b", b"", b"bad\x00"])
def test_rust_rejects_invalid_path_components(tmp_path, native, part):
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            native.native.open_directory_at(fd, [part])
        assert os.fstat(fd)
    finally:
        os.close(fd)


def test_workspace_contract_fresh_links_hidden_trees_and_aliases(tmp_path, native):
    root = tmp_path / "workspace"
    dependency = root / ".venv/deep"
    dependency.mkdir(parents=True)
    (dependency / "file").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a").touch()
    (outside / "b").hardlink_to(outside / "a")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    (root / "dangling").symlink_to(root / "missing")
    os.mkfifo(root / "fifo")  # macOS preflight checks hard links, unlike Linux special-file policy.
    python = object.__new__(MacOSNativeBackend)
    python.workspace = root
    rust = object.__new__(MacOSNativeBackend)
    rust.workspace, rust.directory_backend = root, native
    python._check_workspace()
    rust._check_workspace()
    (tmp_path / "new-link").hardlink_to(dependency / "file")
    errors = []
    for backend in (python, rust):
        with pytest.raises(ValueError, match="硬链接") as caught:
            backend._check_workspace()
        errors.append(str(caught.value))
    assert errors[0] == errors[1]


def test_workspace_unreadable_directory_fails_closed(tmp_path, native):
    if os.geteuid() == 0:
        pytest.skip("Permission denial requires unprivileged account")
    denied = tmp_path / "denied"
    denied.mkdir()
    denied.chmod(0)
    try:
        with pytest.raises(PermissionError):
            native.check_workspace(tmp_path)
    finally:
        denied.chmod(0o700)


def test_cancelled_scans_do_not_return_partial_results(tmp_path, native):
    with FileAccess(tmp_path, directory_backend=native).activate() as access:
        with cancellation_scope() as context:
            context.cancel()
            with pytest.raises(RunCancelled):
                native.check_workspace(tmp_path)
            with access.read_directory(tmp_path) as reader:
                with pytest.raises(RunCancelled):
                    reader.names()
        assert list(access.iterdir(tmp_path)) == []


def test_selection_is_explicit_and_rejects_old_extension(monkeypatch):
    monkeypatch.setenv("AGENT_NATIVE_SCANNER", "python")
    assert select_directory_backend() is None
    monkeypatch.setenv("AGENT_NATIVE_SCANNER", "invalid")
    with pytest.raises(ValueError):
        select_directory_backend()
    monkeypatch.setenv("AGENT_NATIVE_SCANNER", "rust")
    monkeypatch.setitem(
        __import__("sys").modules, "rust_backend", SimpleNamespace(API_VERSION=1)
    )
    with pytest.raises(RuntimeError, match="版本"):
        select_directory_backend()


def test_macos_setup_wires_selected_backend(monkeypatch, native):
    monkeypatch.setattr("sandbox.macos_native.sys.platform", "darwin")
    monkeypatch.setenv("AGENT_NATIVE_SCANNER", "rust")
    monkeypatch.setattr("sandbox.macos_native.Path.is_file", lambda _: True)
    backend = object.__new__(MacOSNativeBackend)
    backend._platform_setup()
    assert isinstance(backend.directory_backend, RustFilesystem)
