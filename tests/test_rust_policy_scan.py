"""Differential contract tests; install the companion wheel to run native cases."""

import errno
import os
import random
import socket
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from host_support.cancellation import RunCancelled, cancellation_scope
from host_support.path_rules import NameRules
from sandbox.linux_mounts import MountTable
from sandbox.linux_policy import PythonPolicyScanner
from sandbox.policy_scan import PolicyPlan, ScanFailure, ScanRequest
from sandbox.policy_scanners import create_policy_scanner
from sandbox.rust_policy import RustPolicyScanner
from scripts.benchmark_linux_policy import policy_backend
from tools._internal.file_policy import PROTECTED_NAME_RULES


@pytest.fixture(params=[(1, 1), (2, 1), (2, 16), (2, 32), (4, 64)])
def engines(monkeypatch, request):
    workers, batch = request.param
    monkeypatch.setenv("AGENT_SCAN_WORKERS", str(workers))
    monkeypatch.setenv("AGENT_SCAN_BATCH_SIZE", str(batch))
    pytest.importorskip("rust_backend")
    return PythonPolicyScanner(), RustPolicyScanner()


def plan(root, reads=(), protected=(), rules=PROTECTED_NAME_RULES):
    return PolicyPlan.compile(root, tuple(reads), tuple(protected), name_rules=rules)


def request(reads=(), **kwargs):
    return ScanRequest(
        tuple(reads), kwargs.pop("git_read", False), MountTable.read().text, **kwargs
    )


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def counts(metrics):
    return {
        key: ([counts(bucket) for bucket in value] if key == "by_root_mount" else value)
        for key, value in metrics.items()
        if not key.endswith("_ms")
    }


def equivalent(engines, compiled, call):
    results = [engine.scan(compiled, call) for engine in engines]
    assert results[0].masks == results[1].masks
    if engines[1].last_diagnostics.get("workers", 1) == 1:
        assert results[0].git_paths == results[1].git_paths
        assert counts(results[0].metrics) == counts(results[1].metrics)
    else:
        assert sorted(results[0].git_paths, key=str) == list(results[1].git_paths)
        left, right = (counts(result.metrics) for result in results)
        for metrics in (left, right):
            metrics["by_root_mount"].sort(key=lambda b: (b["root"], b["mount"] or ""))
        assert left == right
    assert results[0].metrics.keys() == results[1].metrics.keys()
    return results[1]


def failures(engines, compiled, call):
    errors = []
    for engine in engines:
        with pytest.raises(ScanFailure) as caught:
            engine.scan(compiled, call)
        failure = caught.value
        assert failure.metrics["complete"] is False
        assert failure.metrics["scan_ms"] > 0
        errors.append(failure.error)
    assert type(errors[0]) is type(errors[1])
    if isinstance(errors[0], OSError):
        assert (errors[0].errno, errors[0].filename) == (errors[1].errno, errors[1].filename)
    else:
        assert str(errors[0]) == str(errors[1])
    return errors[1]


@pytest.mark.parametrize("git_read", [False, True])
@pytest.mark.parametrize("fixed", ["canonical", "alias", "both"])
def test_aliases_and_path_specific_masks(engines, tmp_path, git_read, fixed):
    base = tmp_path.resolve()
    root, system, alias = base / "project", base / "usr", base / "lib"
    root.mkdir()
    library = system / "lib"
    library.mkdir(parents=True)
    alias.symlink_to(library, target_is_directory=True)
    for parent in (root, library):
        for name in (
            "ok.py",
            ".ENV",
            "key.PEM",
            ".git/config",
            ".git/logs/data",
            ".git/.env",
            "nested/private/file",
            "nested/private2/.env",
        ):
            touch(parent / name)
    views = {"canonical": (library,), "alias": (alias,), "both": (library, alias)}
    protected = tuple(p / "nested/private" for p in views[fixed])
    result = equivalent(
        engines, plan(root, (system, alias), protected), request((system, alias), git_read=git_read)
    )
    assert result.metrics["directories_reused"] > 0


def test_sparse_alias_reuse_and_new_files_on_every_call(engines, tmp_path):
    base = tmp_path.resolve()
    root, system, alias = base / "project", base / "usr", base / "alias"
    root.mkdir()
    for i in range(200):
        touch(system / f"package-{i % 5}/file-{i}.py")
    alias.symlink_to(system, target_is_directory=True)
    compiled, call = plan(root, (system, alias)), request((system, alias))
    first = equivalent(engines, compiled, call)
    assert first.masks == () and first.metrics["directories_reused"] == 6
    touch(system / "package-1/.env.new")
    touch(root / "src/.env.new")
    second = equivalent(engines, compiled, call)
    assert len(second.masks) == 3
    assert first.metrics["masks"] == 0
    alias.unlink()
    other = base / "other"
    touch(other / ".env.other")
    alias.symlink_to(other, target_is_directory=True)
    equivalent(engines, compiled, call)


@pytest.mark.parametrize("kind", ["hardlink", "fifo", "socket", "protected-link"])
@pytest.mark.parametrize("masked", [False, True])
def test_unsafe_workspace_entries_fail_even_under_masks(engines, tmp_path, kind, masked):
    root = tmp_path.resolve()
    parent = root / ("logs" if masked else "src")
    parent.mkdir()
    target = parent / (".env" if kind == "protected-link" else "entry")
    held = None
    try:
        if kind == "hardlink":
            target.hardlink_to(touch(root / "ordinary"))
        elif kind == "fifo":
            os.mkfifo(target)
        elif kind == "socket":
            held = socket.socket(socket.AF_UNIX)
            # AF_UNIX address lengths are short on macOS. chdir keeps the fixture usable.
            old = Path.cwd()
            try:
                os.chdir(parent)
                try:
                    held.bind("entry")
                except PermissionError:
                    pytest.skip("host sandbox blocks Unix socket creation")
            finally:
                os.chdir(old)
        else:
            target.symlink_to("missing")
        if masked and kind == "protected-link":
            equivalent(engines, plan(root), request())
        else:
            failures(engines, plan(root), request())
    finally:
        if held:
            held.close()


def test_system_protected_symlink_masks_parent_and_never_descends(engines, tmp_path):
    base = tmp_path.resolve()
    root, system = base / "project", base / "usr"
    root.mkdir()
    touch(system / "package/ordinary")
    (system / "package/.env").symlink_to("missing")
    (system / "cycle").symlink_to(system, target_is_directory=True)
    result = equivalent(engines, plan(root, (system,)), request((system,)))
    assert result.masks == (system / "package",)


@pytest.mark.parametrize("workspace_denied", [False, True])
def test_real_permission_denial(engines, tmp_path, workspace_denied):
    if os.geteuid() == 0:
        pytest.skip("permission checks require an unprivileged user")
    base = tmp_path.resolve()
    root, system = base / "project", base / "usr"
    root.mkdir()
    system.mkdir()
    denied = (root if workspace_denied else system) / "private"
    touch(denied / ".env")
    alias = base / "alias"
    alias.symlink_to(system, target_is_directory=True)
    denied.chmod(0)
    try:
        compiled, call = plan(root, (system, alias)), request((system, alias))
        if workspace_denied:
            failures(engines, compiled, call)
        else:
            result = equivalent(engines, compiled, call)
            assert set(result.masks) == {denied, alias / "private"}
    finally:
        denied.chmod(0o700)
    equivalent(engines, compiled, call)


def test_pruned_driver_store_only_scans_explicit_packages(engines, tmp_path):
    base = tmp_path.resolve()
    root, store = base / "project", base / "drivers"
    root.mkdir()
    touch(store / "selected/.env")
    touch(store / "other/.env")
    compiled = plan(root, (store,))
    result = equivalent(
        engines,
        compiled,
        request((store,), pruned_paths=(store,), extra_roots=(store / "selected",)),
    )
    assert result.masks == (store / "selected/.env",)


@pytest.mark.parametrize("non_utf8", [False, True])
def test_unicode_lower_and_non_utf8_paths_are_lossless(engines, tmp_path, non_utf8):
    root = tmp_path.resolve()
    names = ["ΣΟΣ", "İ", "KEY", "日本語.PEM", "é.KEY"]
    if non_utf8:
        names += ["\udc80.KEY", "\udcff.KEY"]
    for name in names:
        try:
            touch(root / name)
        except OSError as error:
            if non_utf8 and error.errno in {errno.EILSEQ, errno.EPERM}:
                pytest.skip("filesystem or host sandbox rejects non-UTF-8 names")
            raise
    rules = NameRules(frozenset(n.lower() for n in names[:3]), suffixes=(".pem", ".key"))
    result = equivalent(engines, plan(root, rules=rules), request())
    assert len(result.masks) == len(names)


def test_mount_snapshot_change_fails_closed(engines, tmp_path):
    compiled = plan(tmp_path.resolve())
    call = replace(request(), mount_snapshot="1 0 0:1 / / rw - ext4 /dev/test rw\n")
    failures(engines, compiled, call)


@pytest.mark.parametrize("seed", range(12))
def test_generated_mixed_trees(engines, tmp_path, seed):
    rng = random.Random(seed)
    base = tmp_path.resolve()
    root, system = base / "project", base / "usr"
    root.mkdir()
    system.mkdir()
    names = ["ordinary", ".env", "token.KEY", ".git", "logs", "nested", "logstuff", "ΣΟΣ", "日本語"]
    for i in range(80):
        parent = rng.choice([root, system]) / f"package-{rng.randrange(5)}" / rng.choice(names)
        touch(parent / f"file-{i}")
    alias = base / "alias"
    alias.symlink_to(system, target_is_directory=True)
    equivalent(
        engines,
        plan(root, (system, alias), (alias / "package-1/nested",)),
        request((system, alias), git_read=bool(seed % 2)),
    )


def test_cancellation_keeps_control_flow_and_no_result(engines, tmp_path):
    for engine in engines:
        with cancellation_scope() as context:
            context.cancel()
            with pytest.raises(ScanFailure) as caught:
                engine.scan(plan(tmp_path.resolve()), request())
        assert isinstance(caught.value.error, RunCancelled)
        assert caught.value.error.context is context
        assert not caught.value.metrics["complete"]


def test_rust_backend_selected_by_composition(engines, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_NATIVE_SCANNER", "rust")
    backend = policy_backend(tmp_path.resolve(), ())
    assert backend._mount_policy((), git_read=False) == ([], [])
    assert isinstance(backend._policy_scanner, RustPolicyScanner)


def test_default_and_invalid_engine(monkeypatch):
    monkeypatch.delenv("AGENT_NATIVE_SCANNER", raising=False)
    assert isinstance(create_policy_scanner(), PythonPolicyScanner)
    with pytest.raises(ValueError, match="python 或 rust"):
        create_policy_scanner("typo")


@pytest.mark.parametrize("root_name", ["project", "logs", ".git"])
@pytest.mark.parametrize("git_read", [False, True])
def test_protected_roots_and_non_directory_read_paths(engines, tmp_path, root_name, git_read):
    base = tmp_path.resolve()
    root = base / root_name
    touch(root / "nested/.env")
    read_file = touch(base / "system-file")
    result = equivalent(
        engines,
        plan(root, (read_file, base / "missing")),
        request((read_file, base / "missing"), git_read=git_read),
    )
    assert result.metrics["workspace_entries_checked"] == 1


def test_cancel_during_native_scan_and_no_descriptor_leak(engines, tmp_path):
    from host_support.cancellation import CancellationContext

    root = tmp_path.resolve()
    for i in range(100):
        touch(root / f"package-{i}/file")

    class CancelDuringScan(CancellationContext):
        calls = 0

        def check(self):
            self.calls += 1
            if self.calls == 2:
                self.cancel()
            super().check()

    fd_directory = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    before = len(list(fd_directory.iterdir()))
    for _ in range(15):
        context = CancelDuringScan()
        with cancellation_scope(context), pytest.raises(ScanFailure) as caught:
            engines[1].scan(plan(root), request())
        assert isinstance(caught.value.error, RunCancelled)
        assert not caught.value.metrics["complete"]
    assert len(list(fd_directory.iterdir())) == before


@pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="Unix scanner")
def test_missing_or_incompatible_extension_is_not_a_silent_fallback(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "rust_backend", None)
    with pytest.raises(RuntimeError, match="未安装或无法加载"):
        create_policy_scanner("rust")
    monkeypatch.setitem(sys.modules, "rust_backend", SimpleNamespace(API_VERSION=-1))
    with pytest.raises(RuntimeError, match="接口版本"):
        create_policy_scanner("rust")
