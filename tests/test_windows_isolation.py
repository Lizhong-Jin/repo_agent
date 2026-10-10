"""Failure injection for Windows lifecycle; Win32 kernel tests live separately."""

import ctypes
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.windows_isolation import (
    AppContainerProfile,
    JobAccounting,
    JobExtendedLimits,
    SecurityCapabilities,
    StartupInfoEx,
)
from host_support.windows_processes import WindowsCleanupError, WindowsIsolation, environment_block


class FakeAPI:
    def __init__(self):
        self.events = []
        self.handles = set()
        self.next_handle = 10
        self.fail = None
        self.running = False
        self.child = False
        self.context = None
        self.on_resume = None
        self.delete_error = False

    def event(self, name):
        self.events.append(name)
        if self.fail == name:
            raise OSError("injected " + name)

    def handle(self, own):
        self.next_handle += 1
        self.handles.add(self.next_handle)
        return own(self.next_handle)

    def create_profile(self):
        self.event("profile")
        return AppContainerProfile("test-unique-profile", 99)

    def profile_directory(self, profile):
        self.event("directory")
        return Path("C:/private/profile")

    def delete_profile(self, profile):
        self.event("delete_profile")
        if self.delete_error:
            raise OSError("injected profile release")

    def free_profile_sid(self, profile):
        self.events.append("free_sid")

    def create_job(self, own):
        job = self.handle(own)
        self.event("job")
        return job

    def create_pipe(self, own, **kwargs):
        read, write = self.handle(own), self.handle(own)
        self.event("pipe")
        return read, write

    def create_suspended(self, profile, job, stdio, command, cwd, environment, own):
        self.event("create")
        self.launch = dict(
            profile=profile, job=job, stdio=stdio, command=command, cwd=cwd, environment=environment
        )
        self.process_handle = self.handle(own)
        thread = self.handle(own)
        self.child = True
        return SimpleNamespace(process=self.process_handle, thread=thread, pid=100)

    def verify(self, process, job, profile):
        self.event("verify")
        if self.context:
            self.context.cancel()

    def resume(self, thread):
        self.event("resume")
        if self.on_resume:
            self.on_resume()

    def poll(self, process):
        self.event("poll")
        return None if self.running else 7

    def read(self, pipe):
        self.event("read")
        return b""

    def terminate_job(self, job):
        self.event("terminate")
        self.running = self.child = False

    def active_processes(self, job):
        self.event("active")
        return int(self.child)

    def close_handle(self, handle):
        self.event("close")
        assert handle in self.handles, "double close"
        self.handles.remove(handle)


@pytest.fixture
def api(monkeypatch):
    fake = FakeAPI()
    monkeypatch.setattr("host_support.windows_processes.WindowsIsolationAPI", lambda: fake)
    return fake


def process(scope, **kwargs):
    return scope.process(
        ["C:/private/tool.exe", "a b", 'a"b'],
        cwd="C:/private",
        environment={"SystemRoot": "C:/Windows"},
        **kwargs,
    )


def test_normal_exit_kills_descendants_verifies_empty_and_closes_all_handles(api):
    with WindowsIsolation() as scope:
        execution = process(scope)
        result = execution.run(timeout_seconds=1)
        assert result.exit_code == 7 and result.cleanup_status == "confirmed"
        assert result.process_group_id is None and result.pid == 100
        assert not api.child and not api.handles
        assert api.events.index("verify") < api.events.index("resume")
        assert api.events.index("terminate") < api.events.index("active")
        with pytest.raises(RuntimeError, match="single-use"):
            execution.run(timeout_seconds=1)
    assert api.events[-2:] == ["delete_profile", "free_sid"]
    assert "PATH=" not in api.launch["environment"]


@pytest.mark.parametrize("stage", ["job", "pipe", "create", "verify", "resume", "poll", "read"])
def test_failures_do_not_fall_back_and_release_resources(api, stage):
    api.fail = stage
    expected = WindowsCleanupError if stage in {"poll", "read"} else OSError
    with pytest.raises(expected):
        with WindowsIsolation() as scope:
            execution = process(scope)
            execution.cleanup_seconds = 0.01
            execution.run(timeout_seconds=1)
    assert not api.handles
    if stage in {"job", "pipe", "create", "verify"}:
        assert "resume" not in api.events
    assert api.events.count("create") <= 1
    # Poll/read failure during cleanup must be conservative, never confirmed.
    if stage in {"poll", "read"}:
        assert execution.last_cleanup_status == "unknown"
        assert "delete_profile" not in api.events


def test_cancellation_before_resume_cleans_suspended_process(api):
    context = CancellationContext()
    api.context = context
    with cancellation_scope(context), pytest.raises(RunCancelled):
        with WindowsIsolation() as scope:
            process(scope).run(timeout_seconds=1)
    assert "resume" not in api.events and "terminate" in api.events
    assert context.report()["cleanup_status"] == "confirmed" and not api.handles
    assert "delete_profile" in api.events


def test_cancellation_after_resume_is_propagated_after_job_cleanup(api):
    context = CancellationContext()
    api.on_resume = context.cancel
    with cancellation_scope(context), pytest.raises(RunCancelled):
        with WindowsIsolation() as scope:
            process(scope).run(timeout_seconds=1)
    assert api.events.index("resume") < api.events.index("terminate")
    assert context.report()["cleanup_status"] == "confirmed"


def test_already_cancelled_does_not_allocate_identity(api):
    context = CancellationContext()
    context.cancel()
    with cancellation_scope(context), pytest.raises(RunCancelled), WindowsIsolation():
        pytest.fail("entered")
    assert api.events == []


@pytest.mark.parametrize("failure", ["terminate", "active", "close"])
def test_unconfirmed_cleanup_retains_profile_and_marks_cancellation_unknown(api, failure):
    api.on_resume = lambda: setattr(api, "fail", failure)
    context = CancellationContext()
    with cancellation_scope(context), pytest.raises(WindowsCleanupError, match="retained profile"):
        with WindowsIsolation() as scope:
            execution = process(scope)
            execution.cleanup_seconds = 0.01
            result = execution.run(timeout_seconds=1)
            assert result.cleanup_error and result.cleanup_status == "unknown"
    assert "delete_profile" not in api.events
    assert context.report()["cleanup_status"] == "unknown"


def test_profile_failure_before_process_creation_overrides_original_error(api):
    api.fail, api.delete_error = "directory", True
    with pytest.raises(WindowsCleanupError, match="profile cleanup"):
        with WindowsIsolation():
            pytest.fail("entered")
    assert api.events[-1] == "free_sid"


def test_timeout_cleans_job_without_waiting_for_inherited_pipes(api, monkeypatch):
    clock = iter([0, 0, 2, 2, 2, 2, 2, 2, 2])
    monkeypatch.setattr("host_support.windows_processes.time.monotonic", lambda: next(clock, 2))
    api.running = True
    with WindowsIsolation() as scope:
        result = process(scope).run(timeout_seconds=1)
    assert result.timed_out and result.exit_code is None and result.cleanup_status == "confirmed"
    assert not result.output_complete and not api.handles


@pytest.mark.parametrize(
    "command",
    ["cmd.exe", "C:tool.exe", "\\\\server\\tool.exe", "C:/tool.cmd", "C:/tool.exe\x00bad"],
)
def test_launch_requires_explicit_local_executable(api, command):
    with WindowsIsolation() as scope, pytest.raises(ValueError):
        scope.process([command], cwd="C:/private", environment={})
    assert "create" not in api.events


def test_environment_is_explicit_sorted_and_rejects_case_collisions():
    assert environment_block({"z": "2", "A": "1"}) == "A=1\x00z=2\x00\x00"
    with pytest.raises(ValueError, match="duplicate"):
        environment_block({"Path": "safe", "PATH": "unsafe"})


@pytest.mark.parametrize("key", [None, "LOCALAPPDATA", "LocalAppData"])
def test_launch_supplies_private_localappdata_without_inheriting_or_mutating(api, monkeypatch, key):
    monkeypatch.setenv("LOCALAPPDATA", "C:/host/private")
    monkeypatch.setenv("REPO_AGENT_TEST_SECRET", "must-not-inherit")
    environment = {"SystemRoot": "C:/Windows"}
    if key:
        environment[key] = "C:/private/scratch"
    original = dict(environment)
    with WindowsIsolation() as scope:
        execution = scope.process(
            ["C:/private/tool.exe"], cwd="C:/private", environment=environment
        )
        execution.run(timeout_seconds=1)
        expected = environment[key] if key else str(scope.profile.directory)
    entries = dict(item.split("=", 1) for item in api.launch["environment"].split("\x00") if item)
    assert {name.upper(): value for name, value in entries.items()} == {
        "SYSTEMROOT": "C:/Windows",
        "LOCALAPPDATA": expected,
    }
    assert environment == original


def test_x64_sdk_layouts_have_windows_integer_widths():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("x64 ABI check")
    assert ctypes.sizeof(StartupInfoEx) == 112
    assert ctypes.sizeof(JobExtendedLimits) == 144
    assert ctypes.sizeof(JobAccounting) == 48
    assert ctypes.sizeof(SecurityCapabilities) == 24


def test_win32_launch_attributes_are_atomic_restricted_and_explicit(monkeypatch):
    from host_support import windows_isolation as win

    captured = {}

    class Kernel:
        def InitializeProcThreadAttributeList(self, buffer, count, flags, size):
            ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = 256
            return bool(buffer)

        def UpdateProcThreadAttribute(self, buffer, flags, key, value, size, prev, returned):
            captured[key] = ctypes.string_at(value, size)
            return True

        def CreateProcessW(self, exe, line, pa, ta, inherit, flags, env, cwd, si, pi):
            assert exe == "C:/tool.exe" and line.value == 'C:/tool.exe "a b"'
            assert inherit is True
            assert flags & win.CREATE_SUSPENDED and flags & win.EXTENDED_STARTUPINFO_PRESENT
            assert flags & win.CREATE_UNICODE_ENVIRONMENT and flags & win.CREATE_NO_WINDOW
            assert not flags & 0x1000000  # CREATE_BREAKAWAY_FROM_JOB
            startup = ctypes.cast(si, ctypes.POINTER(win.StartupInfoEx)).contents
            assert startup.startup.cb == ctypes.sizeof(win.StartupInfoEx)
            assert (startup.startup.stdin, startup.startup.stdout, startup.startup.stderr) == (
                1,
                2,
                3,
            )
            assert env.value == "KEY=value"
            result = ctypes.cast(pi, ctypes.POINTER(win.ProcessInformation)).contents
            result.process, result.thread, result.pid = 100, 101, 102
            return True

        def DeleteProcThreadAttributeList(self, buffer):
            captured["released"] = True

    api = object.__new__(win.WindowsIsolationAPI)
    api.kernel = Kernel()
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 122, raising=False)
    owned = []
    api.create_suspended(
        AppContainerProfile("test", 123),
        99,
        (1, 2, 3),
        ["C:/tool.exe", "a b"],
        "C:/private",
        "KEY=value\x00\x00",
        owned.append,
    )
    capabilities = win.SecurityCapabilities.from_buffer_copy(captured[win.SECURITY_CAPABILITIES])
    assert capabilities.sid == 123 and capabilities.count == 0 and not capabilities.capabilities
    assert list((win.HANDLE * 3).from_buffer_copy(captured[win.HANDLE_LIST])) == [1, 2, 3]
    assert list((win.HANDLE * 1).from_buffer_copy(captured[win.JOB_LIST])) == [99]
    assert win.DWORD.from_buffer_copy(captured[win.ALL_APPLICATION_PACKAGES_POLICY]).value == 1
    assert ctypes.c_uint64.from_buffer_copy(captured[win.MITIGATION_POLICY]).value & (1 << 28)
    assert owned == [100, 101] and captured["released"]


def test_job_is_unnamed_noninheritable_and_cannot_break_away():
    from host_support import windows_isolation as win

    class Kernel:
        def CreateJobObjectW(self, attributes, name):
            assert attributes is name is None
            return 123

        def SetInformationJobObject(self, job, kind, pointer, size):
            assert kind == 9 and size == ctypes.sizeof(win.JobExtendedLimits)
            limits = ctypes.cast(pointer, ctypes.POINTER(win.JobExtendedLimits)).contents
            assert limits.basic.flags & win.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            assert not limits.basic.flags & (0x800 | 0x1000)  # both breakaway flags
            return True

    api = object.__new__(win.WindowsIsolationAPI)
    api.kernel = Kernel()
    owned = []

    def own(value):
        owned.append(value)
        return value

    assert api.create_job(own) == 123 and owned == [123]


def test_native_adapter_uses_windows_policy_hooks_and_translates_profile_cleanup(api):
    from sandbox.native_execution import NativeCall, NativeCleanupError
    from sandbox.windows_execution import WindowsLaunch, WindowsNativeExecutionAdapter

    class Backend:
        @contextmanager
        def _prepare_windows_call(self, isolation, call):
            assert isolation.profile.directory and call.command == ("trusted-request",)
            yield WindowsLaunch(("C:/private/tool.exe",), Path("C:/private"), {})

    api.delete_error = True
    call = NativeCall(("trusted-request",), None, Path("C:/private"), False, False, 100)
    with pytest.raises(NativeCleanupError, match="profile cleanup"):
        with WindowsNativeExecutionAdapter().prepare(Backend(), call) as execution:
            assert execution.run(timeout_seconds=1).cleanup_status == "confirmed"
