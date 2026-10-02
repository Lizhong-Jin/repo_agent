"""Compact observations and bounded/recyclable scan storage use real Rust I/O."""

import os

import pytest

from host_support.cancellation import RunCancelled, cancellation_scope
from host_support.rust_filesystem import RustFilesystem
from sandbox.linux_mounts import MountTable
from sandbox.linux_policy import PythonPolicyScanner
from sandbox.policy_scan import PolicyPlan, ScanFailure, ScanRequest
from sandbox.rust_policy import RustPolicyScanner
from tools._internal._file_io import file_signature
from tools._internal.file_access import FileAccess
from tools._internal.file_policy import PROTECTED_NAME_RULES


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setenv("AGENT_SCAN_WORKERS", "1")
    module = pytest.importorskip("rust_backend")
    if not hasattr(module, "scan_metadata"):
        pytest.skip("Requires rust-backend 0.4.0 compact capability")
    return RustFilesystem()


def test_compact_metadata_is_exact_immutable_and_outlives_reader(tmp_path, native):
    root = tmp_path.resolve()
    (root / "file").write_bytes(b"contents")
    os.utime(root / "file", ns=(1_234_567_890_123_456_789, 1_234_567_890_987_654_321))
    (root / "link").symlink_to(root / "file")
    (root / "dir").mkdir()
    names = ["file", "missing", "link", "dir", "file"]
    with FileAccess(root, directory_backend=native).activate() as access:
        with access.read_directory(root) as reader:
            compact = reader.scan_metadata(names)
            complete = reader.stat_many(names)
            assert all(isinstance(item, (os.stat_result, OSError)) for item in complete)
        for name, info in zip(names, compact, strict=True):
            if name == "missing":
                assert isinstance(info, FileNotFoundError) and info.filename == name
            else:
                assert file_signature(info) == file_signature((root / name).lstat())
                with pytest.raises(AttributeError):
                    info.st_size = 0


@pytest.mark.parametrize("packed", [b"..\0", b".\0", b"/absolute\0", b"a/b\0", b"\0", b"no-end"])
def test_compact_wire_rejects_invalid_names(tmp_path, native, packed):
    with FileAccess(tmp_path, directory_backend=native).activate() as access:
        with pytest.raises(ValueError):
            native.native.scan_metadata(access.root_fd, packed)
        assert native.native.scan_metadata(access.root_fd, b"") == []


def test_compact_cancellation_is_not_converted_to_entry_error(tmp_path, native):
    with FileAccess(tmp_path, directory_backend=native).activate() as access:
        with cancellation_scope() as context:
            context.cancel()
            with pytest.raises(RunCancelled):
                native.native.scan_metadata(access.root_fd, b"missing\0", context)


def test_wide_directory_blocks_and_policy_metrics_remain_equivalent(tmp_path, native):
    root = tmp_path.resolve()
    for index in range(1200):
        (root / f"{index:04}.txt").touch()
    (root / ".env").touch()
    diagnostics = native.native.profile_workspace(os.fsencode(root))
    assert diagnostics["enumeration_entries_peak"] == 256
    assert diagnostics["enumeration_buffer_bytes_peak"] < 32_768
    assert diagnostics["path_nodes_created"] == diagnostics["path_nodes_peak"] == 1
    assert diagnostics["path_materialized_bytes"] == 0
    assert diagnostics["directory_handles_peak"] <= 3
    plan = PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES)
    request = ScanRequest((), False, MountTable.read().text)
    python = PythonPolicyScanner().scan(plan, request)
    rust = RustPolicyScanner()
    actual = rust.scan(plan, request)
    assert actual.masks == python.masks == (root / ".env",)
    assert actual.git_paths == python.git_paths
    assert actual.metrics.keys() == python.metrics.keys()
    for key in actual.metrics:
        if key != "by_root_mount" and not key.endswith("_ms"):
            assert actual.metrics[key] == python.metrics[key]
    assert rust.last_diagnostics["enumeration_entries_peak"] == 256


def test_completed_branches_release_path_nodes_and_keep_fd_budget(tmp_path, native):
    root = tmp_path.resolve()
    for branch in range(40):
        path = root / str(branch)
        for _ in range(20):
            path /= "d"
        path.mkdir(parents=True)
    diagnostics = native.native.profile_workspace(os.fsencode(root))
    assert diagnostics["path_nodes_created"] == 841
    # Pending siblings + active ancestry + bounded cached paths, not all 841 nodes.
    assert diagnostics["path_nodes_peak"] < 130
    assert diagnostics["directory_handles_peak"] <= 35
    assert diagnostics["pending_tasks_peak"] <= 40
    assert diagnostics["path_name_copied_bytes"] < 3000


def test_unsafe_entry_after_multiple_blocks_still_fails_closed(tmp_path, native):
    root = tmp_path.resolve()
    for index in range(1000):
        (root / f"{index:04}").touch()
    # Linking outside root changes only the late entry's inode, not directory order.
    late = root / os.listdir(root)[-1]
    os.link(late, root.parent / (root.name + "-outside-link"))
    with pytest.raises(ValueError, match="硬链接"):
        native.check_workspace(root)
    plan = PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES)
    with pytest.raises(ScanFailure) as caught:
        RustPolicyScanner().scan(plan, ScanRequest((), False, MountTable.read().text))
    assert caught.value.metrics["complete"] is False


def test_api_two_wheel_without_compact_capability_keeps_full_metadata_fallback(
    tmp_path, monkeypatch
):
    backend = object.__new__(RustFilesystem)
    backend.native = object()
    expected = [tmp_path.stat()]
    monkeypatch.setattr(backend, "stat_many", lambda fd, names: expected)
    assert backend.scan_metadata(42, ["file"]) is expected


def test_metadata_profile_separates_observation_and_conversion(tmp_path, native):
    (tmp_path / "file").touch()
    with FileAccess(tmp_path, directory_backend=native).activate() as access:
        report = native.native.profile_metadata(access.root_fd, b"file\0missing\0")
    assert report["entries"] == 2
    assert report["observe_ms"] >= 0 and report["python_conversion_ms"] >= 0
