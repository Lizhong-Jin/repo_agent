"""Native preparation contracts; actual denial is tested separately on real kernels."""

import json
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from host_support.windows_recovery import RecoveryLease
from sandbox.native_execution import NativeCall
from sandbox.posix_execution import PosixNativeExecutionAdapter
from sandbox.windows_native import WindowsNativeBackend
from tools.permissions import PermissionManager


@pytest.fixture
def prepared(tmp_path):
    workspace, external, control = (tmp_path / name for name in ("project", "outside", "control"))
    for path in (workspace, external, control):
        path.mkdir()
    (external / "script.py").write_text("print('input')")
    manager = PermissionManager(workspace, backend="native")
    identity = manager.grant("external_read", str(external))
    return workspace, external, control, manager, identity


@pytest.mark.parametrize("worker", [False, True])
def test_posix_maps_argv_and_grants_only_private_copy_read_access(prepared, worker):
    workspace, external, control, manager, identity = prepared
    policies = []

    def policy(command, directory, scratch, read_paths, **kw):
        policies.append(read_paths)
        return command

    backend = SimpleNamespace(
        workspace=workspace,
        directory=control,
        read_paths=(),
        project_python=None,
        python=Path(sys.executable),
        runtime=control,
        permissions=manager,
        _sandbox_command=policy,
        _environment=lambda _: {"PATH": ""},
    )
    call = NativeCall(
        None if worker else (sys.executable, str(external / "script.py")),
        {"name": "get_execution_environment", "arguments": {}} if worker else None,
        workspace,
        False,
        False,
        1024,
    )
    with PosixNativeExecutionAdapter().prepare(backend, call) as process:
        paths = json.loads(process.runner.base_env["REPO_AGENT_READ_ROOTS"])
        target = Path(paths[identity])
        assert target in policies[0] and external not in policies[0]
        assert (target / "script.py").is_file()
        if not worker:
            assert process.command[1] == str(target / "script.py")
    assert not target.exists() and not list(control.iterdir())


@pytest.mark.parametrize("state", ["before_start", "cancelled", "unknown", "cleanup_error"])
def test_windows_inputs_inherit_readonly_profile_never_writable_workspace(
    prepared, monkeypatch, state
):
    from sandbox.native_execution import NativeCleanupError

    workspace, external, control, manager, identity = prepared
    backend = object.__new__(WindowsNativeBackend)
    backend.workspace, backend.directory = workspace, control
    backend.permissions = manager
    backend.runtime = control / "runtime"
    backend.runtime.mkdir()
    backend._agent_layout = SimpleNamespace(
        root=control / "python", executable=control / "python/python.exe", read_paths=()
    )
    backend._project_layout = backend._agent_layout
    backend.python = backend._agent_layout.executable
    backend.windows_state_root = control / "state"
    backend.windows_state_root.mkdir()
    backend.system_directory = control / "System32"
    backend._checking_isolation = False
    backend.protected_paths = ()
    sealed = []
    backend.security = SimpleNamespace(
        sid_string=lambda _: "test-sid",
        seal_tree=lambda path, sid, **kw: sealed.append((path, kw.get("writable", False))),
    )

    def python(layout, target):
        target.mkdir(parents=True)
        executable = target / "python.exe"
        executable.write_bytes(b"MZfake")
        return executable, target

    monkeypatch.setattr("sandbox.windows_native.stage_python", python)
    lease = RecoveryLease(backend.windows_state_root)
    profile = control / "profile"
    profile.mkdir()
    isolation = SimpleNamespace(
        profile=SimpleNamespace(directory=profile, sid=1),
        lease=lease,
        execution=None,
        retention_reason=None,
        retain=lease.retain,
    )
    call = NativeCall(("python", str(external / "script.py")), None, workspace, False, True, 1024)

    class StopBeforeLaunch(Exception):
        pass

    try:
        with (
            pytest.raises(NativeCleanupError if state == "cleanup_error" else StopBeforeLaunch),
            backend._prepare_windows_call(isolation, call) as launch,
        ):
            target = Path(json.loads(launch.environment["REPO_AGENT_READ_ROOTS"])[identity])
            assert launch.command[1] == str(target / "script.py")
            assert (profile, False) in sealed
            assert {p for p, writable in sealed if writable} == {
                profile / "workspace",
                profile / "scratch",
            }
            assert not target.is_relative_to(profile / "workspace")
            assert (external / "script.py").read_text() == "print('input')"
            if state != "before_start":
                isolation.execution = SimpleNamespace(
                    started=True,
                    last_cleanup_status="unknown" if state == "unknown" else "confirmed",
                )
            if state == "cleanup_error":

                def fail_cleanup(path):
                    raise OSError("cannot delete inputs")

                monkeypatch.setattr("sandbox.windows_native.shutil.rmtree", fail_cleanup)
            raise StopBeforeLaunch
        assert target.exists() is (state != "cancelled")
        assert (profile / "workspace").exists()
        assert (backend.windows_state_root / "calls" / lease.identity).exists() is (
            state != "before_start"
        )
    finally:
        monkeypatch.undo()
        lease.finish(released=True)


@pytest.mark.parametrize(
    "entry",
    [
        "C:/profile/inputs/id/../../../outside.exe",
        "C:/Windows/System32/../../outside.exe",
        "../outside.exe",
    ],
)
def test_windows_executable_allowlist_rejects_parent_traversal(monkeypatch, entry):
    monkeypatch.setattr("sandbox.windows_native.Path", PureWindowsPath)
    backend = object.__new__(WindowsNativeBackend)
    backend.python = PureWindowsPath("C:/host/python.exe")
    backend.system_directory = PureWindowsPath("C:/Windows/System32")
    with pytest.raises(ValueError, match=r"\.\."):
        backend._command(
            [entry],
            PureWindowsPath("C:/profile/workspace"),
            [(PureWindowsPath("C:/outside"), PureWindowsPath("C:/profile/inputs/id"))],
            PureWindowsPath("C:/profile/project/python.exe"),
            PureWindowsPath("C:/profile/agent/python.exe"),
            (),
        )
