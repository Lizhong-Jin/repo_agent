"""Bounded relative traversal and metadata reuse without weakening file checks."""

import os
from contextlib import closing

import pytest

from host_support import filesystem as host_fs
from host_support.cancellation import RunCancelled, cancellation_scope
from host_support.file_scan import metadata_entries
from tools._internal.file_access import FileAccess
from tools.filesystem import FindFileTool, ListFileTool, SearchFilesTool


@pytest.fixture(params=["python", "rust"])
def backend(request):
    if request.param == "python":
        return None
    extension = pytest.importorskip("rust_backend")
    if extension.FILESYSTEM_API_VERSION != 2:
        pytest.skip("Rebuild the filesystem extension")
    from host_support.rust_filesystem import RustFilesystem

    return RustFilesystem()


def test_batch_metadata_preserves_stat_fields_errors_order_and_links(tmp_path, backend):
    root = tmp_path.resolve()
    (root / "file").write_bytes(b"abc")
    os.utime(root / "file", ns=(1_234_567_890_123_456_789, 1_234_567_890_987_654_321))
    (root / "link").symlink_to(root / "file")
    (root / "directory").mkdir()
    names = ["link", "missing", "file", "directory", "file"]
    with FileAccess(root, directory_backend=backend).activate() as access:
        with access.read_directory(root) as reader:
            results = reader.stat_many(names)
            for name, result in zip(names, results, strict=True):
                if name == "missing":
                    assert isinstance(result, FileNotFoundError)
                    assert result.filename == name
                    continue
                expected = (root / name).lstat()
                assert tuple(result) == tuple(expected)
                for field in dir(expected):
                    if field.startswith("st_"):
                        assert getattr(result, field) == getattr(expected, field), field
            for invalid in ["", ".", "..", "a/b", "a\x00"]:
                with pytest.raises(ValueError):
                    reader.stat_many([invalid])
        with pytest.raises(ValueError, match="closed"):
            reader.stat_many([])


def test_deep_scan_opens_each_directory_once_and_bounds_handles(tmp_path, backend, monkeypatch):
    root = tmp_path.resolve()
    path = root
    for _ in range(80):
        path /= "d"
        path.mkdir()
    (path / "file").write_text("needle")
    calls = []
    original = host_fs.open_directory if backend is None else backend.directory

    def opened(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        host_fs if backend is None else backend,
        "open_directory" if backend is None else "directory",
        opened,
    )
    cached = []
    original_close = host_fs._ScanDirectories.close

    def close(cache):
        assert len(cache.handles) <= 32
        cached.extend(cache.handles.values())
        original_close(cache)

    monkeypatch.setattr(host_fs._ScanDirectories, "close", close)
    with FileAccess(root, directory_backend=backend).activate():
        result = SearchFilesTool(root).execute({"query": "needle"})
    assert result.success and result.data["files_scanned"] == 1
    assert len(calls) <= 81  # formerly 1 + sum(1..80) ancestor opens
    assert cached
    for fd in cached:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "tool,args", [(ListFileTool, {"path": "."}), (FindFileTool, {"pattern": "*"})]
)
def test_list_find_do_not_reopen_parent_or_restat_candidates(
    tmp_path, backend, monkeypatch, tool, args
):
    root = tmp_path.resolve()
    for index in range(300):
        (root / str(index)).write_text("text")
    with FileAccess(root, directory_backend=backend).activate() as access:
        monkeypatch.setattr(access, "stat", lambda *a, **kw: pytest.fail("redundant path stat"))
        result = tool(root).execute(args)
    assert result.success


def test_batching_bounds_lookahead_and_search_closes_cache(tmp_path, backend, monkeypatch):
    root = tmp_path.resolve()
    for index in range(300):
        (root / str(index)).write_text("needle")
    batches = []
    original = host_fs._DescriptorDirectoryReader.scan_metadata

    def stat_many(reader, names):
        batches.append(len(names))
        return original(reader, names)

    monkeypatch.setattr(host_fs._DescriptorDirectoryReader, "scan_metadata", stat_many)
    with FileAccess(root, directory_backend=backend).activate():
        result = SearchFilesTool(root, max_results=1).execute({"query": "needle"})
        assert host_fs._scan_directories.get() is None
    assert result.success and result.data["truncated"]
    assert batches == [128]


def test_scan_cache_is_scoped_and_writes_use_fresh_parent(tmp_path, backend):
    root = tmp_path.resolve()
    (root / "src").mkdir()
    (root / "src/file").write_text("ordinary")
    (root / "outside").mkdir()
    (root / "outside/file").write_text("secret")
    with FileAccess(root, directory_backend=backend).activate() as access:
        with access.scan_directories():
            with access.read_directory(root / "src") as reader:
                assert reader.names() == ["file"]
            (root / "src").rename(root / "old")
            (root / "src").symlink_to(root / "outside", target_is_directory=True)
            with access.read_directory(root / "src") as reader:
                with reader.open_read("file") as stream:
                    assert stream.read() == b"ordinary"
            with pytest.raises(OSError):
                access.unlink(root / "src/file")
        with pytest.raises(OSError), access.read_directory(root / "src"):
            pytest.fail("cache escaped the scan")
    assert (root / "outside/file").read_text() == "secret"


def test_cancelled_batches_and_early_glob_cleanup(tmp_path, backend):
    root = tmp_path.resolve()
    (root / "src").mkdir()
    (root / "src/file").touch()
    with FileAccess(root, directory_backend=backend).activate() as access:
        for glob in (access.glob_entries, access.glob):
            with closing(glob(root, "**/*")) as entries:
                next(entries)
                assert host_fs._scan_directories.get() is not None
            assert host_fs._scan_directories.get() is None
        with access.read_directory(root) as reader, cancellation_scope() as context:
            context.cancel()
            with pytest.raises(RunCancelled):
                reader.stat_many(["src"])


def test_legacy_reader_metadata_fallback():
    class Reader:
        def stat(self, name):
            raise FileNotFoundError(name)

    assert all(isinstance(info, FileNotFoundError) for _, info in metadata_entries(Reader(), ["a"]))


def test_protected_names_are_not_revealed_as_metadata_errors(tmp_path, backend):
    root = tmp_path.resolve()
    (root / ".env").write_text("secret")
    (root / "safe").touch()
    with FileAccess(root, directory_backend=backend).activate():
        result = ListFileTool(root).execute({"path": "."})
    assert result.success
    assert [entry["name"] for entry in result.data["entries"]] == ["safe"]


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "same_size", "parent"])
def test_batched_search_rechecks_actual_opened_file(tmp_path, backend, monkeypatch, replacement):
    root = tmp_path.resolve()
    parent = root / "src"
    parent.mkdir()
    file = parent / "file"
    file.write_text("ordinary")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    secret = outside / "file"
    secret.write_text("secret!!")
    original = host_fs.open_file
    changed = False

    def open_file(name, *args, **kwargs):
        nonlocal changed
        if name == "file" and not changed:
            changed = True
            if replacement == "parent":
                parent.rename(root / "moved")
                parent.symlink_to(outside, target_is_directory=True)
            elif replacement == "same_size":
                before = file.stat()
                file.write_text("secret!!")
                os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
            else:
                file.unlink()
                if replacement == "symlink":
                    file.symlink_to(secret)
                else:
                    file.hardlink_to(secret)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(host_fs, "open_file", open_file)
    with FileAccess(root, directory_backend=backend).activate():
        result = SearchFilesTool(root).execute({"query": "secret"})
    assert changed and result.success
    assert result.data["matches"] == []
