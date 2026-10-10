"""Opt-in real Windows LPAC/Job tests, never substitute emulation for these."""

import ctypes as C
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from host_support.windows_isolation import WindowsIsolationAPI
from host_support.windows_processes import WindowsIsolation
from host_support.windows_recovery import recover_profiles
from host_support.windows_security import PrivateWindowsSecurity

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.getenv("RUN_WINDOWS_ISOLATION_TESTS") != "1",
    reason="Real Windows x64 LPAC/Job verification; set RUN_WINDOWS_ISOLATION_TESTS=1",
)


@pytest.fixture(scope="module")
def probe_exe(tmp_path_factory):
    directory = tmp_path_factory.mktemp("win32-probe")
    source = Path(__file__).parent / "helpers/windows_isolation_probe.c"
    output = directory / "probe.exe"
    # CI initializes the SDK environment. Missing compiler is a failed acceptance,
    # not a skip that could be mistaken for a successful Windows verification.
    result = subprocess.run(
        [
            "cl.exe",
            "/nologo",
            "/W4",
            "/MT",
            str(source),
            f"/Fe:{output}",
            "/link",
            "advapi32.lib",
            "ws2_32.lib",
        ],
        cwd=directory,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return output


@contextmanager
def staged_probe(probe_exe):
    with WindowsIsolation() as isolation:
        directory = isolation.profile.directory
        executable = directory / "probe.exe"
        shutil.copyfile(probe_exe, executable)
        # Only the per-call identity's own storage is used. No ACLs on the user's
        # Python installation or project are changed by these tests.
        environment = {
            "SystemRoot": isolation.api.windows_directory(),
            "TEMP": str(directory),
            "TMP": str(directory),
        }
        yield isolation, executable, environment


def assert_dead(pid, *, wait_ms=0):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [C.c_uint32, C.c_int32, C.c_uint32]
    kernel.OpenProcess.restype = C.c_void_p
    kernel.WaitForSingleObject.argtypes = [C.c_void_p, C.c_uint32]
    kernel.WaitForSingleObject.restype = C.c_uint32
    kernel.CloseHandle.argtypes = [C.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        assert C.get_last_error() == 87  # PID no longer exists; access denied is not evidence.
        return
    try:
        assert kernel.WaitForSingleObject(handle, wait_ms) == 0
    finally:
        kernel.CloseHandle(handle)


def test_real_token_file_network_isolation_and_profile_release(probe_exe, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("host-only")
    all_packages = tmp_path / "all-packages.txt"
    all_packages.write_text("ordinary AppContainers could read this")
    subprocess.run(
        ["icacls.exe", str(tmp_path), "/grant", "*S-1-15-2-1:(OI)(CI)RX"],
        check=True,
        capture_output=True,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with staged_probe(probe_exe) as (scope, executable, environment):
            directory = scope.profile.directory
            result = scope.process(
                [
                    str(executable),
                    "probe",
                    str(secret),
                    str(tmp_path / "outside.txt"),
                    str(all_packages),
                    str(directory / "scratch.txt"),
                    str(listener.getsockname()[1]),
                ],
                cwd=directory,
                environment=environment,
            ).run(timeout_seconds=20)
            assert result.exit_code == 0, result
            report = json.loads(result.stdout)
            assert report == {
                "container": 1,
                "capabilities": 1,  # The launcher verifies the exact registryRead SID.
                "job": 1,
                "read": 5,
                "write": 5,
                "all_packages_read": 5,
                "scratch": 0,
                "network": 10013,
            }, report
            assert result.cleanup_status == "confirmed"
            assert not (tmp_path / "outside.txt").exists()
        assert not directory.exists()


@pytest.mark.parametrize("mode", ["tree", "breakaway", "flood"])
def test_real_descendants_breakaway_and_bounded_output(probe_exe, mode):
    with staged_probe(probe_exe) as (scope, executable, environment):
        result = scope.process(
            [str(executable), mode],
            cwd=scope.profile.directory,
            environment=environment,
            max_output_bytes=1024,
        ).run(timeout_seconds=20)
        assert result.exit_code == 0 and result.cleanup_status == "confirmed", result
        assert_dead(result.pid)
        if mode == "tree":
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            children = [row["child"] for row in rows if "child" in row]
            assert len(children) == 2, rows
            identities = [row["identity"] for row in rows if "identity" in row]
            expected_sid = PrivateWindowsSecurity(scope.api).sid_string(
                C.addressof(scope.api.registry_read_sid)
            )
            assert len(identities) == 3, rows
            assert {row["pid"] for row in identities} == {result.pid, *children}
            for identity in identities:
                assert identity == {
                    "pid": identity["pid"],
                    "container": 1,
                    "all_packages_member": 0,
                    "job": 1,
                    "capability": expected_sid,
                    "attributes": 4,
                }
            for pid in children:
                # Job accounting can reach zero just before descendant handles
                # become signaled. Still require actual death within a bound.
                assert_dead(pid, wait_ms=5000)
        elif mode == "breakaway":
            assert json.loads(result.stdout)["breakaway_error"] == 5
        else:
            assert result.stdout_truncated and result.stderr_truncated
            assert len(result.stdout) < 1200 and len(result.stderr) < 1200


def test_real_timeout_and_cancel(probe_exe):
    with staged_probe(probe_exe) as (scope, executable, environment):
        result = scope.process(
            [str(executable), "sleep"], cwd=scope.profile.directory, environment=environment
        ).run(timeout_seconds=1)
        assert result.timed_out and result.cleanup_status == "confirmed", result
        assert_dead(result.pid)
    context = CancellationContext()
    with staged_probe(probe_exe) as (scope, executable, environment):
        execution = scope.process(
            [str(executable), "sleep"], cwd=scope.profile.directory, environment=environment
        )
        timer = threading.Timer(1, context.cancel)
        timer.start()
        try:
            with cancellation_scope(context), pytest.raises(RunCancelled):
                execution.run(timeout_seconds=20)
        finally:
            timer.cancel()
            timer.join()
        assert context.report()["cleanup_status"] == "confirmed"
        for record in context.report()["cleanups"]:
            if record.get("pid"):
                assert_dead(record["pid"])


def test_real_create_failure_closes_job_and_deletes_profile():
    with WindowsIsolation() as scope:
        directory = scope.profile.directory
        execution = scope.process(
            [str(directory / "missing.exe")],
            cwd=directory,
            environment={"SystemRoot": scope.api.windows_directory()},
        )
        with pytest.raises(OSError):
            execution.run(timeout_seconds=1)
        assert execution.last_cleanup_status == "confirmed"
    assert not directory.exists()


def test_real_api_is_available():
    assert WindowsIsolationAPI().windows_directory()


def test_real_minimal_environment_gets_private_localappdata():
    with WindowsIsolation() as scope:
        directory = scope.profile.directory
        executable = directory / "cmd.exe"
        shutil.copyfile(Path(scope.api.windows_directory()) / "System32/cmd.exe", executable)
        result = scope.process(
            [str(executable), "/d", "/c", "echo %LOCALAPPDATA%"],
            cwd=directory,
            environment={"SystemRoot": scope.api.windows_directory()},
        ).run(timeout_seconds=10)
        assert result.exit_code == 0 and result.cleanup_status == "confirmed", result
        # Windows may append Packages/<profile>/AC while constructing the child
        # environment. The resulting location must remain inside this scope.
        assert Path(result.stdout.strip()).is_relative_to(directory)
        assert_dead(result.pid)
    assert not directory.exists()


def test_real_supervisor_death_kills_entire_job(probe_exe, tmp_path):
    log = tmp_path / "supervisor.jsonl"
    # The helper host is deliberately killed without finally blocks. Each line is
    # flushed outside the LPAC so readiness does not depend on process completion.
    code = r"""
import json, shutil, sys
from dataclasses import asdict
from pathlib import Path
from host_support.windows_processes import WindowsIsolation
with open(sys.argv[2], "w", encoding="utf-8", buffering=1, newline="") as log:
    with WindowsIsolation(recovery_root=Path(sys.argv[3])) as scope:
        exe = scope.profile.directory / "probe.exe"
        shutil.copyfile(sys.argv[1], exe)
        log.write(json.dumps({"profile": scope.profile.name}) + "\n")
        create, read = scope.api.create_suspended, scope.api.read
        def created(*args):
            info = create(*args)
            log.write(json.dumps({"leader": info.pid}) + "\n")
            return info
        def drained(pipe):
            data = read(pipe)
            if data:
                log.write(data.decode("utf-8"))
            return data
        scope.api.create_suspended, scope.api.read = created, drained
        result = scope.process([str(exe), "middle"], cwd=scope.profile.directory,
                               environment={"SystemRoot": scope.api.windows_directory()}).run(
                                   timeout_seconds=60)
        log.write(json.dumps({"probe_result": asdict(result)}) + "\n")
        raise RuntimeError(f"Probe returned before supervisor was killed: {result!r}")
"""
    supervisor = subprocess.Popen(
        [sys.executable, "-c", code, str(probe_exe), str(log), str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    rows = []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if log.exists():
                lines = log.read_text(encoding="utf-8").splitlines()
                try:
                    rows = [json.loads(line) for line in lines if line.strip()]
                except ValueError:  # Writer may be between writes of one line.
                    continue
                if any("ready" in row for row in rows):
                    break
            assert supervisor.poll() is None, f"Supervisor exited before readiness: {rows}"
            time.sleep(0.02)
        assert any("ready" in row for row in rows), f"Supervisor did not become ready: {rows}"
        supervisor.kill()
        supervisor.communicate(timeout=5)
        # Job termination is asynchronous after the last host handle closes.
        pids = [row[key] for row in rows for key in ("leader", "child") if key in row]
        deadline = time.monotonic() + 5
        while True:
            try:
                for pid in pids:
                    assert_dead(pid)
                break
            except AssertionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        failure = sys.exception()
        cleanup_errors = []
        try:
            if supervisor.poll() is None:
                supervisor.kill()
            stdout, stderr = supervisor.communicate(timeout=5)
            if failure is not None:
                failure.add_note(
                    f"Supervisor exit={supervisor.returncode}; stdout={stdout!r}; stderr={stderr!r}"
                )
        except Exception as error:
            cleanup_errors.append(("Supervisor shutdown", error))
        try:
            report = recover_profiles(tmp_path, WindowsIsolationAPI())
            assert not report["failed"], report
            assert not list(tmp_path.glob("*.json")), report
        except Exception as error:
            cleanup_errors.append(("Profile recovery", error))
        try:
            log.unlink(missing_ok=True)
        except OSError as error:
            cleanup_errors.append(("Supervisor log removal", error))
        if cleanup_errors:
            primary = failure if failure is not None else cleanup_errors[0][1]
            for stage, error in cleanup_errors:
                primary.add_note(f"{stage}: {type(error).__name__}: {error}")
            if failure is None:
                raise primary
    # Only a completed kill/death check requires crash recovery. An early startup
    # failure can already have deleted its profile through normal scope cleanup.
    expected = [row["profile"] for row in rows if "profile" in row]
    assert report["removed"] == expected, report
