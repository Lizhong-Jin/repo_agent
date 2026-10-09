"""One-call Windows isolation and bounded Job cleanup, independent of tool policy.

An isolation scope owns a fresh LPAC profile. The caller must prepare authorized
files before running; this module never grants access to the host workspace or
inherits the host environment. A scope is not a complete native backend.
"""

import ntpath
import time
from collections.abc import Sequence

from .cancellation import current_cancellation
from .processes import ProcessResult, ProcessRunner, _BoundedCapture
from .windows_isolation import WindowsIsolationAPI
from .windows_recovery import RecoveryLease


class WindowsCleanupError(RuntimeError):
    """Isolation resources could not be released; do not reuse this scope."""


def _absolute_local_path(value):
    value = str(value)
    drive, tail = ntpath.splitdrive(value)
    if (
        len(drive) != 2
        or drive[1] != ":"
        or not drive[0].isalpha()
        or not tail.startswith(("\\", "/"))
        or "\x00" in value
    ):
        raise ValueError("Windows isolation requires an absolute local drive path")
    return value


def environment_block(environment):
    snapshot = ProcessRunner._validate_environment(environment)
    names = [key.upper() for key in snapshot]
    if len(set(names)) != len(names):
        raise ValueError("Windows environment has case-insensitive duplicate names")
    return (
        "\x00".join(f"{key}={snapshot[key]}" for key in sorted(snapshot, key=str.upper))
        + "\x00\x00"
    )


class WindowsIsolation:
    """Trusted resource scope. Each instance permits exactly one process launch.

    Use as a context manager, stage authorized files under profile.directory,
    then call process(...).run(...). User/tool input must not create scopes or
    supply environment/ACL policies directly. No network capabilities are added.
    """

    def __init__(self, *, recovery_root=None):
        self.api = WindowsIsolationAPI()
        self.profile = None
        self.execution = None
        self.closed = False
        self.recovery_root = recovery_root
        self.lease = None
        self.retention_reason = None

    def __enter__(self):
        if self.profile is not None or self.closed:
            raise RuntimeError("Windows isolation scopes cannot be reused")
        cancellation = current_cancellation()
        if cancellation:
            cancellation.check()
        try:
            if self.recovery_root is not None:
                self.lease = RecoveryLease(self.recovery_root)
            self.profile = (
                self.api.create_profile(self.lease.name)
                if self.lease
                else self.api.create_profile()
            )
            self.profile.directory = self.api.profile_directory(self.profile)
        except BaseException:
            if self.profile is not None:
                self.__exit__(None, None, None)
            elif self.lease:
                self.lease.finish(released=False)
            raise
        return self

    def retain(self, reason):
        """Keep unpublished workspace changes; the automatic reaper must skip them."""
        self.retention_reason = str(reason)
        if self.lease:
            self.lease.retain(reason)

    def process(self, command, *, cwd, environment, max_output_bytes=32 * 1024):
        if self.profile is None or self.closed or self.execution is not None:
            raise RuntimeError("Windows isolation requires a fresh, open scope")
        self.execution = WindowsNativeProcess(self, command, cwd, environment, max_output_bytes)
        return self.execution

    def __exit__(self, exc_type, exc, traceback):
        if self.closed:
            return
        self.closed = True
        error = None
        try:
            if self.execution and self.execution.last_cleanup_status == "unknown":
                # Keep the profile when live processes cannot be ruled out. Never
                # delete files or revoke resources from a potentially live tree.
                error = "Process cleanup unknown; retained profile " + self.profile.name
            elif self.retention_reason is None:
                self.api.delete_profile(self.profile)
        except Exception as failure:
            error = f"AppContainer profile cleanup failed: {failure}"
        finally:
            self.api.free_profile_sid(self.profile)
            if self.lease:
                try:
                    self.lease.finish(released=not error and self.retention_reason is None)
                except (OSError, ValueError) as failure:
                    error = f"Windows isolation recovery record cleanup failed: {failure}"
        if error:
            cancellation = current_cancellation()
            if cancellation:
                cancellation.record_cleanup(
                    "unknown", source="windows_profile", error=error, profile=self.profile.name
                )
            if self.execution:
                self.execution.last_cleanup_status = "unknown"
            # Also signal failure before prepare() has yielded a process, where
            # native_common cannot inspect last_cleanup_status yet.
            raise WindowsCleanupError(error) from exc


class WindowsNativeProcess:
    """Implements the native process contract using LPAC and an atomic Job launch."""

    cleanup_seconds = 3.0

    def __init__(self, scope, command, cwd, environment, max_output_bytes):
        if (
            isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or not command
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
        ):
            raise ValueError("command must be a non-empty sequence of strings without NUL")
        _absolute_local_path(command[0])
        if not command[0].lower().endswith(".exe"):
            raise ValueError("Windows isolation requires an explicit .exe entrypoint")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be positive")
        self.scope, self.api = scope, scope.api
        self.command, self.cwd = tuple(command), _absolute_local_path(cwd)
        self.environment = environment_block(environment)
        self.max_output_bytes = max_output_bytes
        self.last_cleanup_status = "not_needed"
        self.diagnostics = []
        self.used = False
        self.result = None
        self.started = False

    def run(self, *, timeout_seconds):
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        if self.used or self.scope.closed:
            raise RuntimeError("Windows native processes are single-use")
        self.used = True
        cancellation = current_cancellation()
        if cancellation:
            cancellation.check()
        started = time.monotonic()
        captures = [_BoundedCapture(self.max_output_bytes), _BoundedCapture(self.max_output_bytes)]
        handles, streams = [], {}
        job = information = None
        exit_code, timed_out = None, False

        def own(handle):
            handles.append(handle)
            return handle

        def close(handle):
            self.api.close_handle(handle)
            handles.remove(handle)

        try:
            job = (
                self.api.create_job(own, self.scope.lease.job)
                if self.scope.lease
                else self.api.create_job(own)
            )
            input_read, input_write = self.api.create_pipe(own, child_reads=True)
            output_read, output_write = self.api.create_pipe(own)
            error_read, error_write = self.api.create_pipe(own)
            streams = {output_read: captures[0], error_read: captures[1]}
            close(input_write)  # stdin is EOF; no interactive input channel.
            self.last_cleanup_status = "unknown"
            information = self.api.create_suspended(
                self.scope.profile,
                job,
                (input_read, output_write, error_write),
                self.command,
                self.cwd,
                self.environment,
                own,
            )
            for handle in (input_read, output_write, error_write):
                close(handle)
            self.api.verify(information.process, job, self.scope.profile)
            if cancellation:
                cancellation.check()
            timed_out = time.monotonic() >= started + timeout_seconds
            if not timed_out:
                self.started = True
                self.api.resume(information.thread)
                close(information.thread)
            while not timed_out:
                if cancellation:
                    cancellation.check()
                self._drain(streams)
                exit_code = self.api.poll(information.process)
                if exit_code is not None:
                    break
                if time.monotonic() >= started + timeout_seconds:
                    timed_out = True
                    break
                time.sleep(0.01)
        finally:
            # No cancellation checkpoints here. Normal exit also kills residual
            # descendants, even if they detached or closed their output handles.
            self._cleanup(job, information, handles, streams)
            if cancellation:
                cancellation.record_cleanup(
                    self.last_cleanup_status,
                    source="windows_job",
                    pid=information.pid if information else None,
                    diagnostics=list(self.diagnostics),
                )
        cleanup_error = (
            "Windows Job/handle cleanup was not confirmed"
            if self.last_cleanup_status == "unknown"
            else None
        )
        self.result = ProcessResult(
            exit_code=None if timed_out else exit_code,
            stdout=captures[0].text(),
            stderr=captures[1].text(),
            timed_out=timed_out,
            cleanup_error=cleanup_error,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout_truncated=captures[0].truncated,
            stderr_truncated=captures[1].truncated,
            status="timed_out" if timed_out else "completed",
            cleanup_status=self.last_cleanup_status,
            output_complete=not (timed_out or cleanup_error or any(c.truncated for c in captures)),
            pid=information.pid,
            process_group_id=None,
            cleanup_diagnostics=list(self.diagnostics),
        )
        return self.result

    def _drain(self, streams):
        for pipe, capture in list(streams.items()):
            data = self.api.read(pipe)
            if data:
                capture.feed(data)
            elif data == b"":
                del streams[pipe]

    def _failure(self, stage, error):
        self.diagnostics.append(
            {"stage": stage, "error": str(error), "winerror": getattr(error, "winerror", None)}
        )

    def _cleanup(self, job, information, handles, streams):
        empty = job is None
        deadline = time.monotonic() + self.cleanup_seconds
        try:
            # A failed launch still leaves the parent's copies of child pipe ends
            # open. Close them before draining, otherwise our own handles hide EOF.
            retained = {job, information.process if information else None, *streams}
            for handle in list(handles):
                if handle not in retained:
                    try:
                        self.api.close_handle(handle)
                        handles.remove(handle)
                    except Exception as error:
                        self._failure("close_child_handle", error)
            if job is not None:
                try:
                    self.api.terminate_job(job)
                except Exception as error:
                    self._failure("terminate_job", error)
                while True:
                    try:
                        empty = self.api.active_processes(job) == 0
                    except Exception as error:
                        self._failure("query_job", error)
                        break
                    if empty or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
            # A leader exit or successful TerminateJobObject alone is not proof.
            if not empty:
                self._failure("wait_job", "Job still contains processes or cannot be queried")
            if information:
                while self.api.poll(information.process) is None:
                    if time.monotonic() >= deadline:
                        self._failure("wait_process", "Process handle is not signaled")
                        break
                    time.sleep(0.01)
            while streams and time.monotonic() < deadline:
                self._drain(streams)
                if streams:
                    time.sleep(0.001)
            if streams:
                self._failure("output", "Output pipes did not reach EOF")
        except Exception as error:
            self._failure("cleanup", error)
        finally:
            # Job closes last and is never inherited: host death also kills it.
            for handle in reversed(handles):
                try:
                    self.api.close_handle(handle)
                except Exception as error:
                    self._failure("close_handle", error)
            self.last_cleanup_status = "unknown" if self.diagnostics else "confirmed"
