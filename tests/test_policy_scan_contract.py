"""Scanner batch contract: fresh observations, failure diagnostics and backend injection."""

import errno

import pytest

from host_support.path_rules import NameRules
from sandbox.linux_mounts import MountTable
from sandbox.linux_policy import PythonPolicyScanner
from sandbox.policy_scan import PolicyPlan, ScanFailure, ScanRequest, ScanResult
from scripts.benchmark_linux_policy import policy_backend
from tools._internal.file_policy import PROTECTED_NAME_RULES


def request(*paths):
    return ScanRequest(tuple(paths), False, MountTable.read().text)


def test_engine_reuses_configuration_but_observes_new_files_and_keeps_old_results(tmp_path):
    root = tmp_path.resolve()
    engine = PythonPolicyScanner()
    plan = PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES)
    first = engine.scan(plan, request())
    (root / ".env.new").touch()
    second = engine.scan(plan, request())
    assert first.masks == ()
    assert second.masks == (root / ".env.new",)
    assert first.metrics["workspace_entries_checked"] == 0
    assert second.metrics["workspace_entries_checked"] == 1
    assert first.metrics is not second.metrics


def test_engine_failure_has_diagnostics_without_partial_result(tmp_path):
    root = tmp_path.resolve()
    (root / ".env").symlink_to("missing")
    with pytest.raises(ScanFailure) as caught:
        PythonPolicyScanner().scan(
            PolicyPlan.compile(root, (), (), name_rules=PROTECTED_NAME_RULES), request()
        )
    assert isinstance(caught.value.error, ValueError)
    assert not caught.value.metrics["complete"]
    assert caught.value.metrics["scan_ms"] > 0


def test_engine_uses_injected_name_rules(tmp_path):
    root = tmp_path.resolve()
    (root / "private.data").touch()
    (root / ".env").touch()
    plan = PolicyPlan.compile(root, (), (), name_rules=NameRules(frozenset({"private.data"})))
    result = PythonPolicyScanner().scan(plan, request())
    assert result.masks == (root / "private.data",)
    assert result.metrics["workspace_entries_checked"] == 2


@pytest.mark.parametrize("metrics", [{}, {"complete": False}])
def test_incomplete_results_cannot_cross_batch_boundary(metrics):
    with pytest.raises(ValueError, match="incomplete"):
        ScanResult((), (), metrics)


def test_native_adapter_uses_only_the_batch_contract(tmp_path):
    root = tmp_path.resolve()
    backend = policy_backend(root, ())
    seen = []
    metrics = {"complete": True, "scan_ms": 0}

    class Scanner:
        def scan(self, plan, call):
            seen.append((plan, call))
            return ScanResult((root / "hidden",), (), metrics)

    backend._policy_scanner = Scanner()
    assert backend._mount_policy((), git_read=True) == ([root / "hidden"], [])
    plan, call = seen.pop()
    assert plan.workspace == root
    assert call.git_read and call.read_paths == ()
    assert call.mount_snapshot == MountTable.read().text
    backend.last_policy_metrics["materialization_ms"] = 1
    assert "materialization_ms" not in metrics


@pytest.mark.parametrize(
    "error", [PermissionError(errno.EACCES, "denied", "/x"), KeyboardInterrupt()]
)
def test_adapter_preserves_failure_kind_and_metrics(tmp_path, error):
    backend = policy_backend(tmp_path.resolve(), ())
    metrics = {"complete": False, "scan_ms": 1}

    class Scanner:
        def scan(self, plan, call):
            raise ScanFailure(error, metrics)

    backend._policy_scanner = Scanner()
    with pytest.raises(type(error)) as caught:
        backend._mount_policy((), git_read=False)
    assert caught.value is error
    assert backend.last_policy_metrics == metrics
