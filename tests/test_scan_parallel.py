"""Bounded unordered scans must retain the complete safety decision and fd ownership."""

import os
from pathlib import Path

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.rust_filesystem import RustFilesystem
from sandbox.linux_mounts import MountTable
from sandbox.policy_scan import PolicyPlan, ScanFailure, ScanRequest
from sandbox.rust_policy import RustPolicyScanner
from tools._internal.file_policy import PROTECTED_NAME_RULES


@pytest.fixture
def native():
    module = pytest.importorskip("rust_backend")
    if getattr(module, "SCAN_BATCH_VERSION", None) != 1:
        pytest.skip("Requires rust-backend 0.6.0")
    return module


def policy(root):
    scanner = RustPolicyScanner()
    result = scanner.scan(
        PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES),
        ScanRequest((), True, MountTable.read().text),
    )
    return result, scanner.last_diagnostics


@pytest.mark.parametrize("workers", [1, 2, 4, 8])
def test_branching_tree_bounds_and_stable_policy(tmp_path, monkeypatch, native, workers):
    monkeypatch.setenv("AGENT_SCAN_WORKERS", str(workers))
    root = tmp_path.resolve()
    for index in range(24):
        branch = root / f"package-{index:02}"
        (branch / ".git/nested/.git").mkdir(parents=True)
        (branch / ".env").touch()
        (branch / ".git/config").touch()
        (branch / "link").symlink_to(root, target_is_directory=True)
    workspace = native.profile_workspace(os.fsencode(root))
    result, diagnostics = policy(root)
    assert len(result.masks) == 24 and len(result.git_paths) == 48
    assert result.metrics["workspace_directories_scanned"] == 97
    assert result.metrics["complete"] is True
    for report in (workspace, diagnostics):
        assert report["workers"] == workers
        assert 1 <= report["in_flight_peak"] <= workers
        if workers > 1:
            assert report["in_flight_peak"] == workers
        assert report["enumeration_entries_peak"] <= 256
    assert workspace["directory_handles_peak"] <= (35 if workers == 1 else 33 + 3 * workers)
    assert diagnostics["directory_handles_peak"] <= workers
    for _ in range(3):
        again, _ = policy(root)
        assert again.masks == result.masks
        assert again.git_paths == result.git_paths


@pytest.mark.parametrize("value", ["0", "9", "-1", "many", "2.5"])
def test_invalid_limit_fails_closed(tmp_path, monkeypatch, native, value):
    monkeypatch.setenv("AGENT_SCAN_WORKERS", value)
    with pytest.raises(ValueError, match="AGENT_SCAN_WORKERS"):
        native.check_workspace(os.fsencode(tmp_path))
    with pytest.raises(ScanFailure) as caught:
        policy(tmp_path.resolve())
    assert isinstance(caught.value.error, ValueError)
    assert not caught.value.metrics["complete"]


def test_default_is_two(tmp_path, monkeypatch, native):
    monkeypatch.delenv("AGENT_SCAN_WORKERS", raising=False)
    assert native.profile_workspace(os.fsencode(tmp_path))["workers"] == 2
    assert policy(tmp_path.resolve())[1]["workers"] == 2
    monkeypatch.setenv("AGENT_SCAN_WORKERS", "")
    assert native.profile_workspace(os.fsencode(tmp_path))["workers"] == 2


@pytest.mark.parametrize("scenario", ["workspace", "policy"])
@pytest.mark.parametrize("workers", [2, 4])
def test_active_cancellation_and_unsafe_branches_do_not_leak(
    tmp_path, monkeypatch, native, scenario, workers
):
    monkeypatch.setenv("AGENT_SCAN_WORKERS", str(workers))
    root = tmp_path.resolve()
    for index in range(40):
        branch = root / str(index)
        branch.mkdir()
        for entry in range(300):
            (branch / str(entry)).touch()
    fd_root = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")

    def scan():
        return RustFilesystem().check_workspace(root) if scenario == "workspace" else policy(root)

    class CancelDuringScan(CancellationContext):
        calls = 0

        def check(self):
            self.calls += 1
            if self.calls == 2:
                self.cancel()
            super().check()

    before = len(list(fd_root.iterdir()))
    for _ in range(5):
        context = CancelDuringScan()
        with cancellation_scope(context):
            if scenario == "workspace":
                with pytest.raises(RunCancelled) as caught:
                    scan()
                assert caught.value.context is context
            else:
                with pytest.raises(ScanFailure) as caught:
                    scan()
                assert isinstance(caught.value.error, RunCancelled)
                assert caught.value.error.context is context
                assert not caught.value.metrics["complete"]
        assert len(list(fd_root.iterdir())) == before
    # Multiple offending branches: which path wins is deliberately unspecified.
    for index in (3, 17, 32):
        os.link(root / str(index) / "0", root.parent / f"{root.name}-{index}-outside")
    for _ in range(5):
        with pytest.raises((ValueError, ScanFailure)) as caught:
            scan()
        error = caught.value.error if isinstance(caught.value, ScanFailure) else caught.value
        assert isinstance(error, ValueError) and "硬链接" in str(error)
        assert len(list(fd_root.iterdir())) == before


@pytest.mark.parametrize("batch_size", [1, 16, 32, 64])
def test_batch_limits_and_reduced_directory_round_trips(tmp_path, native, monkeypatch, batch_size):
    root = tmp_path.resolve()
    for index in range(256):
        directory = root / f"package-{index}"
        directory.mkdir()
        for entry in range(8):
            (directory / (".env" if entry == 0 else str(entry))).touch()
    monkeypatch.setenv("AGENT_SCAN_WORKERS", "2")
    monkeypatch.setenv("AGENT_SCAN_BATCH_SIZE", str(batch_size))
    workspace = native.profile_workspace(os.fsencode(root))
    result, diagnostics = policy(root)
    assert len(result.masks) == 256
    assert result.metrics["directories_scanned"] == 257
    assert result.metrics["workspace_entries_checked"] == 2048
    for report in (workspace, diagnostics):
        assert report["directories_submitted"] == 256
        assert 1 <= report["batch_directories_peak"] <= batch_size
        assert report["in_flight_directories_peak"] <= 2 * batch_size
        assert report["in_flight_peak"] <= 2
        if batch_size == 1:
            assert report["batches_submitted"] == 256
        else:
            assert report["batches_submitted"] < 256 // 4
    assert workspace["directory_handles_peak"] <= 39
    assert diagnostics["directory_handles_peak"] <= 2


@pytest.mark.parametrize("value", ["0", "65", "-1", "many", "2.5"])
def test_invalid_batch_size_fails_closed(tmp_path, native, monkeypatch, value):
    monkeypatch.setenv("AGENT_SCAN_BATCH_SIZE", value)
    with pytest.raises(ValueError, match="AGENT_SCAN_BATCH_SIZE"):
        native.check_workspace(os.fsencode(tmp_path))
    with pytest.raises(ScanFailure) as caught:
        policy(tmp_path.resolve())
    assert not caught.value.metrics["complete"]


def test_default_batch_size_and_serial_baseline(tmp_path, native, monkeypatch):
    monkeypatch.delenv("AGENT_SCAN_BATCH_SIZE", raising=False)
    monkeypatch.delenv("AGENT_SCAN_WORKERS", raising=False)
    result = native.profile_workspace(os.fsencode(tmp_path))
    assert result["workers"] == 2 and result["batch_size"] == 32
    for value in ("", "64"):
        monkeypatch.setenv("AGENT_SCAN_BATCH_SIZE", value)
        monkeypatch.setenv("AGENT_SCAN_WORKERS", "1")
        report = native.profile_workspace(os.fsencode(tmp_path))
        assert report["batches_submitted"] == report["directories_submitted"] == 0
