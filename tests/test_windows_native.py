"""Windows native policy, publication and crash-recovery contracts on every host."""

import json
import os
from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled
from host_support.processes import ProcessResult
from host_support.windows_recovery import RecoveryLease, recover_profiles
from host_support.windows_security import PrivateWindowsSecurity
from sandbox.native_execution import NativeCall, NativeCleanupError
from sandbox.windows_git import GitQueryProcess, copy_git_runtime, validate_git_query
from sandbox.windows_native import WindowsNativeBackend
from sandbox.windows_python import inspect_python, stage_python
from sandbox.windows_workspace import WindowsCallSnapshot, copy_private_tree


def test_git_runtime_copies_hardlinks_by_value_without_optional_helpers(tmp_path):
    source = tmp_path / "git-bin"
    source.mkdir()
    original = tmp_path / "original"
    original.write_bytes(b"MZshared")
    os.link(original, source / "git.exe")
    os.link(original, source / "dependency.dll")
    (source / "git-lfs.exe").write_text("unneeded")
    before = original.stat()
    target = tmp_path / "private"
    copy_git_runtime(source, target)
    assert set(p.name for p in target.iterdir()) == {"git.exe", "dependency.dll"}
    for file in target.iterdir():
        assert file.read_bytes() == b"MZshared"
        assert file.stat().st_nlink == 1 and not os.path.samefile(file, original)
        file.write_bytes(b"private edit")
    assert original.read_bytes() == b"MZshared"
    assert original.stat().st_mode == before.st_mode and original.stat().st_nlink == 3
    # The general copier still refuses the same hard-linked source.
    with pytest.raises(ValueError, match="link or special"):
        copy_private_tree(source, tmp_path / "strict")


def test_git_runtime_rejects_reparse_source_and_honors_size_limit(tmp_path):
    source = tmp_path / "bin"
    source.mkdir()
    (source / "git.exe").write_bytes(b"MZcontent")
    with pytest.raises(ValueError, match="size limit"):
        copy_git_runtime(source, tmp_path / "small", limit=2)
    try:
        (source / "bad.dll").symlink_to(source / "git.exe")
    except OSError:
        pytest.skip("Creating symlinks requires Windows Developer Mode")
    with pytest.raises(ValueError, match="link or special"):
        copy_git_runtime(source, tmp_path / "linked")


@pytest.mark.parametrize("system_packages", [False, True])
def test_venv_base_packages_follow_config_and_own_packages_are_preserved(tmp_path, system_packages):
    root = tmp_path / "project"
    root.mkdir()
    base_python = python_install(tmp_path / "base")
    (base_python.parent / "Lib/site-packages").mkdir()
    (base_python.parent / "Lib/site-packages/base_only.py").write_text("VALUE=1")
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts/python.exe").write_bytes(b"MZredirector")
    (venv / "Lib/site-packages").mkdir(parents=True)
    (venv / "Lib/site-packages/own.py").write_text("VALUE=2")
    config = f"home = {base_python.parent}\ninclude-system-site-packages = {system_packages}\n"
    (venv / "pyvenv.cfg").write_text(config)
    layout = inspect_python(venv / "Scripts/python.exe", "explicit", root)
    relocated, base = stage_python(layout, tmp_path / "staged")
    assert (base / "Lib/site-packages/base_only.py").exists() is system_packages
    assert (base / "Lib/encodings").is_dir()
    assert (relocated.parent.parent / "Lib/site-packages/own.py").is_file()
    assert (venv / "pyvenv.cfg").read_text() == config
    # A directly selected base Python still carries its installed packages.
    direct = inspect_python(base_python, "base", root)
    _, direct_base = stage_python(direct, tmp_path / "direct")
    assert (direct_base / "Lib/site-packages/base_only.py").exists()


@pytest.mark.parametrize("name", ["git_status", "git_diff", "git_log", "git_show"])
def test_git_queries_use_host_validation_and_isolated_direct_runner(tmp_path, name):
    from host_support.processes import ProcessStartError

    backend = object.__new__(WindowsNativeBackend)
    backend.workspace = tmp_path
    backend.healthy = True
    backend._prepare_workspace = lambda: None
    calls = []

    def run(command, **options):
        calls.append((command, options))
        raise ProcessStartError(FileNotFoundError("test Git unavailable"))

    backend._run = run
    assert not backend._execute(tmp_path, name, {"cwd": "../"}).success
    assert not calls  # Validation precedes any launch.
    assert not backend._execute(tmp_path, name, {}).success
    command, options = calls[0]
    assert command[0] == "git" and command[-2:] == ["rev-parse", "--show-toplevel"]
    assert options["git_read"] is True and "request" not in options


@pytest.mark.parametrize(
    "command",
    [
        ["git", "checkout", "main"],
        ["git", "config", "core.hooksPath", "bad"],
        ["git", "-c"],
        ["python", "-c", "pass"],
    ],
)
def test_git_compatibility_rejects_non_query_commands(command):
    with pytest.raises(ValueError):
        validate_git_query(command)


def test_arbitrary_git_command_does_not_request_compatibility_policy(tmp_path):
    from host_support.processes import ProcessStartError
    from tools.execute import RunCommandTool

    backend = object.__new__(WindowsNativeBackend)
    backend.workspace = tmp_path
    backend.healthy = True
    backend._prepare_workspace = lambda: None
    backend._tool_catalog = lambda: {
        "run_command": RunCommandTool(tmp_path, execution_allowed=True)
    }
    calls = []

    def run(command, **options):
        calls.append(options)
        raise ProcessStartError(FileNotFoundError("Git is not in standard runtime"))

    backend._run = run
    assert not backend._execute(tmp_path, "run_command", {"command": ["git", "status"]}).success
    assert len(calls) == 1 and not calls[0].get("git_read", False)


def test_git_discovery_maps_only_private_repository_root(tmp_path):
    private = tmp_path / "private"
    workspace = tmp_path / "original"
    result = ProcessResult(0, str(private / "nested") + "\n", "", False, None, 1, False, False)
    process = SimpleNamespace(last_cleanup_status="confirmed", run=lambda **kw: result)
    call = NativeCall(("git", "rev-parse", "--show-toplevel"), None, workspace, True, False, 100)
    mapped = GitQueryProcess(process, call, private, workspace).run(timeout_seconds=1)
    assert mapped.stdout == str(workspace / "nested") + "\n"
    other = NativeCall(("git", "log"), None, workspace, True, False, 100)
    assert GitQueryProcess(process, other, private, workspace).run(timeout_seconds=1) == result


def test_git_launch_is_direct_readonly_and_does_not_stage_python(tmp_path, monkeypatch):
    backend = object.__new__(WindowsNativeBackend)
    backend.workspace = tmp_path / "project"
    backend.workspace.mkdir()
    metadata = backend.workspace / ".git"
    (metadata / "objects/info").mkdir(parents=True)
    (metadata / "HEAD").write_text("ref: refs/heads/main\n")
    (metadata / "objects/info/alternates").write_text("C:/outside")
    (metadata / "config").write_text('[filter "bad"]\nprocess=bad.exe\n')
    (metadata / "hooks").mkdir()
    (metadata / "hooks/pre-commit").write_text("bad.exe")
    backend.directory = tmp_path / "backend"
    backend.runtime = tmp_path / "unused-runtime"
    backend._agent_layout = SimpleNamespace(read_paths=())
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
    installed = tmp_path / "Git"
    (installed / "mingw64/bin").mkdir(parents=True)
    (installed / "mingw64/bin/git.exe").write_bytes(b"MZgit")
    monkeypatch.setattr(
        "sandbox.windows_native.find_windows_executable",
        lambda *a, **kw: str(installed / "cmd/git.exe"),
    )

    def unexpected_python(*args):
        pytest.fail("Git queries must not stage or launch Python")

    monkeypatch.setattr("sandbox.windows_native.stage_python", unexpected_python)
    monkeypatch.setattr(WindowsCallSnapshot, "publish", unexpected_python)
    lease = RecoveryLease(backend.windows_state_root)
    profile = tmp_path / "profile"
    profile.mkdir()
    scope = SimpleNamespace(
        profile=SimpleNamespace(directory=profile, sid=1),
        lease=lease,
        execution=None,
        retention_reason=None,
        retain=lease.retain,
    )
    call = NativeCall(
        ("git", "rev-parse", "--show-toplevel"), None, backend.workspace, True, False, 100
    )
    try:
        with backend._prepare_windows_call(scope, call) as launch:
            assert launch.git_query and launch.command[0] == str(
                profile / "git-runtime/bin/git.exe"
            )
            assert not (profile / "agent-python").exists() and not (profile / "runtime").exists()
            private_metadata = launch.cwd / ".git"
            assert (private_metadata / "HEAD").read_text().startswith("ref:")
            assert "bad.exe" not in (private_metadata / "config").read_text()
            assert not (private_metadata / "hooks").exists()
            assert not (private_metadata / "objects/info/alternates").exists()
            assert (launch.cwd, {"writable": False}) in sealed
            assert launch.environment["GIT_LITERAL_PATHSPECS"] == "1"
            assert launch.environment["GIT_CONFIG_VALUE_2"] == "never"
            scope.execution = SimpleNamespace(
                result=object(), last_cleanup_status="confirmed", started=True
            )
        assert not lease.record["retain"]
        assert backend.last_writeback["files"] == []
        assert "bad.exe" in (metadata / "config").read_text()
    finally:
        lease.finish(released=False)


@pytest.mark.parametrize(
    "failure",
    ["startup", "malformed_json", "check", "timeout", "cleanup", "stdout", "stderr", "unhealthy"],
)
def test_preflight_failures_keep_bounded_diagnostics(tmp_path, failure):
    backend = object.__new__(WindowsNativeBackend)
    backend.directory = tmp_path
    backend.python = tmp_path / "python.exe"
    backend.healthy = failure != "unhealthy"
    report = dict.fromkeys(
        ["host_read", "host_write", "runtime_write", "network", "workspace_write"], True
    )
    if failure == "check":
        report["network"] = False
    stdout = json.dumps(report)
    if failure in {"startup", "malformed_json"}:
        stdout = "invalid-output" * 1000
    result = ProcessResult(
        exit_code=0xC0000022 if failure == "startup" else None if failure == "timeout" else 0,
        stdout=stdout,
        stderr="loader diagnostic " * 1000,
        timed_out=failure == "timeout",
        cleanup_error="job still active" if failure == "cleanup" else None,
        duration_ms=1,
        stdout_truncated=failure == "stdout",
        stderr_truncated=failure == "stderr",
        cleanup_status="unknown" if failure == "cleanup" else "confirmed",
    )
    backend._run = lambda *args, **kwargs: result
    with pytest.raises(ValueError, match="Windows native 隔离自检失败") as caught:
        backend._preflight()
    diagnostic = json.loads(str(caught.value).split("：", 1)[1])
    assert diagnostic["exit_code"] == result.exit_code
    if failure == "startup":
        assert diagnostic["exit_code_hex"] == "0xC0000022"
    assert diagnostic["timed_out"] == result.timed_out
    assert diagnostic["cleanup_status"] == result.cleanup_status
    assert diagnostic["healthy"] == backend.healthy
    assert diagnostic["stdout"] == stdout[:1000]
    assert diagnostic["stderr"] == result.stderr[:1000]
    if failure == "check":
        assert "'network': False" in diagnostic["checks"]
    assert not backend._checking_isolation
    assert not (tmp_path / "host-only.txt").exists()


def test_preflight_accepts_complete_isolation_report(tmp_path):
    backend = object.__new__(WindowsNativeBackend)
    backend.directory, backend.python, backend.healthy = tmp_path, tmp_path / "python.exe", True
    report = dict.fromkeys(
        ["host_read", "host_write", "runtime_write", "network", "workspace_write"], True
    )
    backend._run = lambda *args, **kwargs: ProcessResult(
        0, json.dumps(report), "", False, None, 1, False, False, cleanup_status="confirmed"
    )
    backend._preflight()
    assert backend.preflight_metrics == {"isolation_verified": True, "checks": report}
    assert not (tmp_path / "host-only.txt").exists()


@pytest.mark.parametrize("denied_at", ["create", "connect", None])
def test_preflight_network_probe_handles_creation_and_connection_denial(tmp_path, denied_at):
    import builtins

    backend = object.__new__(WindowsNativeBackend)
    backend.directory, backend.python, backend.healthy = tmp_path, tmp_path / "python.exe", True
    secret = str(tmp_path / "host-only.txt")

    class ProbePath:
        def __init__(self, name):
            self.name = name

        def __truediv__(self, name):
            return ProbePath(self.name + "/" + name)

        def read_bytes(self):
            raise PermissionError("host read denied")

        def write_text(self, value):
            if self.name == secret or self.name.startswith("private-runtime/"):
                raise PermissionError("protected write denied")

        def read_text(self):
            return "allowed"

    class ProbeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, seconds):
            pass

        def connect(self, address):
            if denied_at == "connect":
                raise PermissionError("connection denied")

    def socket_factory():
        if denied_at == "create":
            raise PermissionError("socket creation denied")
        return ProbeSocket()

    modules = {
        "json": json,
        "os": SimpleNamespace(environ={"AGENT_PRIVATE_RUNTIME": "private-runtime"}),
        "pathlib": SimpleNamespace(Path=ProbePath),
        "socket": SimpleNamespace(socket=socket_factory),
    }

    def run(command, **kwargs):
        output = []
        namespace = {
            "__builtins__": {
                **vars(builtins),
                "__import__": lambda name, *args, **kw: modules[name],
                "print": output.append,
            }
        }
        # Execute the actual generated preflight, including the socket context.
        exec(command[-1], namespace)
        return ProcessResult(
            0, output[0], "", False, None, 1, False, False, cleanup_status="confirmed"
        )

    backend._run = run
    if denied_at is None:
        with pytest.raises(ValueError, match="'network': False"):
            backend._preflight()
    else:
        backend._preflight()
        assert backend.preflight_metrics["checks"]["network"]


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
@pytest.mark.parametrize("kind", ["command", "probe", "worker"])
def test_prepared_windows_call_publishes_after_confirmed_cleanup(
    tmp_path, monkeypatch, outcome, kind
):
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

    def unexpected_git(*args):
        pytest.fail("Python, interpreter probes and LSP workers must not stage Git")

    monkeypatch.setattr(backend, "_stage_git", unexpected_git)
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
        (str(backend.python), "-c", "pass") if kind != "worker" else None,
        {"name": "get_symbols", "arguments": {"path": "file.txt"}} if kind == "worker" else None,
        backend.workspace,
        False,
        kind != "probe",
        100,
    )
    try:

        def run():
            with backend._prepare_windows_call(scope, call) as launch:
                assert str(profile) in launch.command[0]
                assert not launch.git_query
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
