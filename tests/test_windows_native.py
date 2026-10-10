"""Windows native policy, publication and crash-recovery contracts on every host."""

import json
import os
from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled
from host_support.windows_recovery import RecoveryLease, recover_profiles
from host_support.windows_security import PrivateWindowsSecurity
from sandbox.native_execution import NativeCall, NativeCleanupError
from sandbox.windows_native import WindowsNativeBackend
from sandbox.windows_python import inspect_python, stage_python
from sandbox.windows_workspace import WindowsCallSnapshot, copy_private_tree


class RecoveryAPI:
    def __init__(self):
        self.deleted = []
        self.empty = True
        self.fail = False

    def job_is_empty(self, name):
        assert name.startswith("Global\\repo-agent-native-")
        if self.fail:
            raise PermissionError("Job query failed")
        return self.empty

    def recover_profile(self, name):
        self.deleted.append(name)


def test_recovery_lease_preserves_live_calls_and_collects_after_abandonment(tmp_path):
    api = RecoveryAPI()
    lease = RecoveryLease(tmp_path)
    assert recover_profiles(tmp_path, api)["active"] == [lease.name]
    assert not api.deleted
    lease.finish(released=False)
    assert recover_profiles(tmp_path, api)["removed"] == [lease.name]
    assert not lease.path.exists() and len(api.deleted) == 1


def test_recovery_retains_unpublished_work_until_explicit_discard(tmp_path):
    api = RecoveryAPI()
    lease = RecoveryLease(tmp_path)
    lease.retain("interrupted publication")
    lease.finish(released=False)
    assert recover_profiles(tmp_path, api)["retained"] == [lease.name]
    api.empty = False
    assert recover_profiles(tmp_path, api, discard=lease.identity)["active"] == [lease.name]
    assert lease.path.exists() and not api.deleted
    api.empty = True
    assert recover_profiles(tmp_path, api, discard=lease.identity)["removed"] == [lease.name]


def test_recovery_does_not_delete_on_query_failure_or_invalid_record(tmp_path):
    api = RecoveryAPI()
    lease = RecoveryLease(tmp_path)
    lease.finish(released=False)
    api.fail = True
    assert recover_profiles(tmp_path, api)["failed"]
    api.fail = False
    lease.path.write_text(json.dumps({"version": 1, "name": "another-app", "retain": False}))
    assert recover_profiles(tmp_path, api)["failed"] and not api.deleted
    assert lease.path.exists()


def test_recovery_preview_never_deletes(tmp_path):
    api = RecoveryAPI()
    lease = RecoveryLease(tmp_path)
    lease.finish(released=False)
    assert recover_profiles(tmp_path, api, dry_run=True)["eligible"] == [lease.name]
    assert lease.path.exists() and not api.deleted


def test_private_acl_prevents_readonly_owner_tampering():
    security = object.__new__(PrivateWindowsSecurity)
    security.user_sid = "S-1-5-21-1"
    readonly = security.sddl("S-1-15-2-1")
    writable = security.sddl("S-1-15-2-1", writable=True)
    assert "D:P" in readonly and "(D;OICI;0xd0156;;;S-1-15-2-1)" in readonly
    assert "(A;OICI;RC;;;OW)" in readonly
    assert "0x1301bf" in writable and "0xc0000" in writable
    assert "S-1-15" not in security.sddl()


def test_snapshot_filters_credentials_and_preserves_original_on_conflict(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "file.txt").write_text("baseline")
    (root / ".env").write_text("secret")
    (root / "private.cfg").write_text("configured secret")
    (root / "empty").mkdir()
    snapshot = WindowsCallSnapshot(
        root, tmp_path / "workspace", tmp_path / "control", protected_paths=(root / "private.cfg",)
    )
    assert not (snapshot.workspace / ".env").exists()
    assert not (snapshot.workspace / "private.cfg").exists()
    assert (snapshot.workspace / "empty").is_dir()
    (snapshot.workspace / "file.txt").write_text("command update")
    (root / "file.txt").write_text("concurrent edit")
    with pytest.raises(ValueError, match="已发生变化"):
        snapshot.publish()
    assert (root / "file.txt").read_text() == "concurrent edit"
    assert (snapshot.workspace / "file.txt").read_text() == "command update"


def test_snapshot_publishes_only_unprotected_changes_and_records_backup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "old.txt").write_text("before")
    snapshot = WindowsCallSnapshot(root, tmp_path / "workspace", tmp_path / "control")
    (snapshot.workspace / "old.txt").unlink()
    (snapshot.workspace / "new.txt").write_text("after")
    (snapshot.workspace / ".env").write_text("never export")
    assert snapshot.publish() == ["new.txt", "old.txt"]
    assert (root / "new.txt").read_text() == "after"
    assert not (root / "old.txt").exists() and not (root / ".env").exists()
    manifest = json.loads((snapshot.last_backup / "manifest.json").read_text())
    assert manifest["status"] == "complete"


def test_snapshot_refuses_links_before_publication(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "data").write_text("x")
    snapshot = WindowsCallSnapshot(root, tmp_path / "workspace", tmp_path / "control")
    os.link(snapshot.workspace / "data", snapshot.workspace / "other")
    with pytest.raises(ValueError, match="非普通文件"):
        snapshot.publish()
    assert (root / "data").read_text() == "x" and not (root / "other").exists()


def python_install(path):
    (path / "Lib/encodings").mkdir(parents=True)
    (path / "Lib/encodings/__init__.py").write_text("# test")
    (path / "python.exe").write_bytes(b"MZtest")
    return path / "python.exe"


def test_python_relocation_authorizes_only_runtime_layout_without_host_execution(tmp_path):
    base = tmp_path / "runtimes/python"
    python_install(base)
    (base / "unrelated.txt").write_text("not a runtime file")
    root = tmp_path / "project"
    root.mkdir()
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts/python.exe").write_bytes(b"MZredirector")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\n")
    layout = inspect_python(venv / "Scripts/python.exe", "explicit", root)
    executable, relocated_base = stage_python(layout, tmp_path / "stage")
    assert executable.name == "python.exe" and executable.is_file()
    assert f"home = {relocated_base}" in (executable.parent.parent / "pyvenv.cfg").read_text()
    assert not (relocated_base / "unrelated.txt").exists()
    assert (venv / "pyvenv.cfg").read_text() == f"home = {base}\n"


def test_python_rejects_broad_or_invalid_layouts(tmp_path):
    root = tmp_path / "project"
    executable = python_install(root)
    with pytest.raises(ValueError, match="broad"):
        inspect_python(executable, "explicit", root)
    with pytest.raises(ValueError, match="python.exe"):
        inspect_python(root / "wrapper.exe", "explicit", tmp_path / "other/project")


@pytest.mark.parametrize("use_venv", [False, True])
@pytest.mark.parametrize("link_kind", ["hardlink", "symlink"])
def test_python_relocation_omits_optional_python3_links(tmp_path, use_venv, link_kind):
    outside = tmp_path / "outside.exe"
    outside.write_bytes(b"must not be read or copied")

    def alias(path):
        if link_kind == "hardlink":
            os.link(outside, path)
        else:
            try:
                path.symlink_to("python.exe")
            except OSError as error:
                if getattr(error, "winerror", None) == 1314:
                    pytest.skip(
                        "Creating symbolic links requires Windows developer mode or privilege"
                    )
                raise

    base = tmp_path / "runtimes/python"
    executable = python_install(base)
    alias(base / "python3.exe")
    if use_venv:
        environment = tmp_path / "project/.venv"
        (environment / "Scripts").mkdir(parents=True)
        executable = environment / "Scripts/python.exe"
        executable.write_bytes(b"MZredirector")
        alias(environment / "Scripts/python3.exe")
        (environment / "pyvenv.cfg").write_text(f"home = {base}\n")
    layout = inspect_python(executable, "explicit", tmp_path / "project")
    relocated, relocated_base = stage_python(layout, tmp_path / "stage")
    assert relocated.read_bytes() == executable.read_bytes()
    assert not os.path.lexists(relocated.parent / "python3.exe")
    assert not os.path.lexists(relocated_base / "python3.exe")
    assert (base / "python3.exe").exists()
    assert outside.read_bytes() == b"must not be read or copied"


@pytest.mark.parametrize("relative", ["python.exe", "Lib/encodings/__init__.py"])
def test_python_relocation_still_rejects_required_runtime_links(tmp_path, relative):
    base = tmp_path / "runtimes/python"
    executable = python_install(base)
    layout = inspect_python(executable, "explicit", tmp_path / "project")
    os.link(base / relative, tmp_path / "outside-link")
    with pytest.raises(ValueError, match="link"):
        stage_python(layout, tmp_path / "stage")


def test_runtime_copy_rejects_hardlinks_and_filters_secrets(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / ".env").write_text("secret")
    (root / "data").write_text("data")
    copy_private_tree(root, tmp_path / "safe")
    assert not (tmp_path / "safe/.env").exists()
    os.link(root / "data", root / "alias")
    with pytest.raises(ValueError, match="link"):
        copy_private_tree(root, tmp_path / "unsafe")


@pytest.mark.parametrize("outcome", ["success", "conflict", "cancel", "unknown", "startup_failure"])
def test_prepared_windows_call_publishes_after_confirmed_cleanup(tmp_path, monkeypatch, outcome):
    backend = object.__new__(WindowsNativeBackend)
    backend.workspace = tmp_path / "project"
    backend.workspace.mkdir()
    (backend.workspace / "file.txt").write_text("before")
    backend.directory = tmp_path / "control"
    backend.runtime = tmp_path / "runtime"
    backend.runtime.mkdir()
    (backend.runtime / "worker.py").write_text("# trusted")
    backend.python = python_install(tmp_path / "installed-python")
    backend._agent_layout = inspect_python(backend.python, "agent fallback", backend.workspace)
    backend._project_layout = backend._agent_layout
    backend.windows_state_root = tmp_path / "state"
    backend.windows_state_root.mkdir()
    backend.system_directory = tmp_path / "System32"
    backend.protected_paths = ()
    backend._checking_isolation = False
    sealed = []
    backend.security = SimpleNamespace(
        sid_string=lambda sid: "test-SID",
        seal_tree=lambda path, sid, **kw: sealed.append((path, kw)),
    )
    monkeypatch.setattr(backend, "_stage_git", lambda *args: ())
    lease = RecoveryLease(backend.windows_state_root)
    profile = tmp_path / "profile"
    profile.mkdir()
    scope = SimpleNamespace(
        profile=SimpleNamespace(directory=profile, sid=1),
        lease=lease,
        execution=None,
        retention_reason=None,
    )

    def retain(reason):
        scope.retention_reason = reason
        lease.retain(reason)

    scope.retain = retain
    call = NativeCall(
        (str(backend.python), "-c", "pass"), None, backend.workspace, False, True, 100
    )
    try:

        def run():
            with backend._prepare_windows_call(scope, call) as launch:
                assert str(profile) in launch.command[0]
                assert launch.environment["HOME"] == str(profile / "scratch")
                (launch.cwd / "file.txt").write_text("command result")
                if outcome == "conflict":
                    (backend.workspace / "file.txt").write_text("external edit")
                scope.execution = SimpleNamespace(
                    result=object(),
                    last_cleanup_status="unknown" if outcome == "unknown" else "confirmed",
                    started=outcome != "startup_failure",
                )

                if outcome == "cancel":
                    raise RunCancelled(CancellationContext())
                if outcome == "startup_failure":
                    raise OSError("CreateProcess failed")

        if outcome in {"conflict", "unknown", "cancel"}:
            error = RunCancelled if outcome == "cancel" else NativeCleanupError
            with pytest.raises(error):
                run()
            assert lease.record["retain"] and (profile / "workspace/file.txt").exists()
            assert (backend.windows_state_root / "calls" / lease.identity).exists()
            expected = "external edit" if outcome == "conflict" else "before"
            assert (backend.workspace / "file.txt").read_text() == expected
        elif outcome == "startup_failure":
            with pytest.raises(OSError, match="CreateProcess failed"):
                run()
            assert not lease.record["retain"]
            assert not (backend.windows_state_root / "calls" / lease.identity).exists()
        else:
            run()
            assert (backend.workspace / "file.txt").read_text() == "command result"
            assert backend.last_writeback["files"] == ["file.txt"] and not lease.record["retain"]
        assert sealed[0][0] == profile  # Read-only root before writable subtrees.
    finally:
        lease.finish(released=False)


def test_recovery_reports_missing_files_during_cleanup_as_failure(tmp_path):
    lease = RecoveryLease(tmp_path)
    lease.finish(released=False)

    class BrokenAPI(RecoveryAPI):
        def recover_profile(self, name):
            raise FileNotFoundError("partial cleanup")

    assert recover_profiles(tmp_path, BrokenAPI())["failed"]
    assert lease.path.exists()


def test_failed_snapshot_preparation_removes_host_control(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    control = tmp_path / "control"
    with pytest.raises(ValueError, match="protected directory"):
        WindowsCallSnapshot(root, tmp_path / "private", control, protected_paths=(root,))
    assert not control.exists()


def test_release_removes_control_before_forgetting_ownership(tmp_path, monkeypatch):
    lease = RecoveryLease(tmp_path)
    control = tmp_path / "calls" / lease.identity
    control.mkdir(parents=True)
    (control / "state.json").write_text("{}")

    def fail(path):
        raise PermissionError("backup still open")

    with monkeypatch.context() as patch:
        patch.setattr("host_support.windows_recovery.shutil.rmtree", fail)
        with pytest.raises(PermissionError, match="backup still open"):
            lease.finish(released=True)
    assert lease.path.exists() and control.exists()
    assert recover_profiles(tmp_path, RecoveryAPI())["removed"] == [lease.name]
    assert not control.exists() and not lease.path.exists()
