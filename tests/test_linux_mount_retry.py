"""Only observed mount churn can restart a policy scan, before process launch."""

from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from sandbox.linux_mounts import MOUNT_LAYOUT_CHANGED, MountTable
from sandbox.linux_policy import PythonPolicyScanner
from sandbox.policy_scan import ScanFailure
from scripts.benchmark_linux_policy import policy_backend


@pytest.fixture
def scanning(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    backend = policy_backend(root, (), ())
    version = [0]

    def table():
        return MountTable(f"{version[0]} 0 0:1 / / rw - ext4 /dev/test rw\n")

    monkeypatch.setattr(MountTable, "read", table)
    return backend, root, version


def test_changed_mount_restarts_from_fresh_filesystem_observations(scanning):
    backend, root, version = scanning
    scanner = PythonPolicyScanner()
    calls = []

    def scan(plan, request):
        calls.append(request.mount_snapshot)
        if len(calls) == 1:
            (root / "new.key").write_text("must be hidden on retry")
            version[0] += 1
        return scanner.scan(plan, request)

    backend._policy_scanner = SimpleNamespace(scan=scan)
    masks, _ = backend._mount_policy((), git_read=False)
    assert masks == [root / "new.key"]
    assert len(calls) == 2 and calls[0] != calls[1]
    assert backend.last_policy_metrics["complete"]


@pytest.mark.parametrize("change", [False, True])
def test_unchanged_or_continuously_changing_mounts_still_fail_closed(scanning, change):
    backend, _, version = scanning
    calls = []

    def scan(*args):
        calls.append(1)
        version[0] += int(change)
        raise ScanFailure(ValueError(MOUNT_LAYOUT_CHANGED), {"complete": False})

    backend._policy_scanner = SimpleNamespace(scan=scan)
    with pytest.raises(ValueError, match=MOUNT_LAYOUT_CHANGED):
        backend._mount_policy((), git_read=False)
    assert len(calls) == (3 if change else 1)
    assert not backend.last_policy_metrics["complete"]


def test_other_errors_are_not_retried(scanning):
    backend, _, version = scanning
    calls = []

    def scan(*args):
        calls.append(1)
        version[0] += 1
        raise ScanFailure(PermissionError("denied"), {"complete": False})

    backend._policy_scanner = SimpleNamespace(scan=scan)
    with pytest.raises(PermissionError):
        backend._mount_policy((), git_read=False)
    assert len(calls) == 1


def test_cancellation_stops_before_retry(scanning):
    backend, _, version = scanning
    context = CancellationContext()
    calls = []

    def scan(*args):
        calls.append(1)
        version[0] += 1
        context.cancel()
        raise ScanFailure(ValueError(MOUNT_LAYOUT_CHANGED), {"complete": False})

    backend._policy_scanner = SimpleNamespace(scan=scan)
    with cancellation_scope(context), pytest.raises(RunCancelled):
        backend._mount_policy((), git_read=False)
    assert len(calls) == 1
