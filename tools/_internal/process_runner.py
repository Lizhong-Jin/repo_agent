"""Reusable subprocess execution; callers own workspace and authorization policies.

This module does not depend on tool schemas, LLM types, or ToolResult. Running a
command does not restrict its filesystem access to its working directory.
"""

import logging
import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessResult:
    """Execution outcome; nonzero exits and timeouts are returned, not raised."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cleanup_error: str | None
    duration_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    status: str = "completed"
    cleanup_status: str = "not_needed"
    output_complete: bool = True
    pid: int | None = None
    process_group_id: int | None = None
    cleanup_diagnostics: list[dict[str, Any]] = field(default_factory=list)


class ProcessStartError(RuntimeError):
    """The OS could not start a process; cause retains the original exception."""

    def __init__(self, cause: OSError | RuntimeError) -> None:
        super().__init__("Unable to start the process.")
        self.cause = cause


class _BoundedCapture:
    """Keep bounded head/tail bytes while fully draining a subprocess stream."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.head = bytearray()
        self.tail = bytearray()
        self.head_limit = max_bytes // 2
        self.tail_limit = max_bytes - self.head_limit
        self.total_bytes = 0

    def feed(self, data: bytes) -> None:
        """Feed data into the capture buffer."""
        self.total_bytes += len(data)
        if len(self.head) < self.head_limit:
            remaining_head = self.head_limit - len(self.head)
            self.head.extend(data[:remaining_head])
            take = min(remaining_head, len(data))
            data = data[take:]
        if len(data) > 0:
            self.tail.extend(data)
            if len(self.tail) > self.tail_limit:
                self.tail = self.tail[-self.tail_limit :]

    @property
    def truncated(self) -> bool:
        """Whether the capture was truncated."""
        return self.total_bytes > self.max_bytes

    def text(self) -> str:
        """Return the captured text as a string."""
        if self.truncated:
            return (
                self.head.decode("utf-8", errors="replace")
                + "\n... (output truncated) ...\n"
                + self.tail.decode("utf-8", errors="replace")
            )
        else:
            raw = bytes(self.head + self.tail)
            return raw.decode("utf-8", errors="replace")


class ProcessRunner:
    """Execute argv commands with bounded output, timeouts, and process cleanup.

    Each run owns its output buffers and process. Environment settings are copied
    at construction. No workspace or credential-path policy is applied here.
    """

    def __init__(
        self,
        *,
        max_output_bytes: int = 32 * 1024,
        base_env: Mapping[str, str] | None = None,
        supervise_tree: bool = False,
    ) -> None:
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        self.max_output_bytes = max_output_bytes
        self.supervise_tree = supervise_tree
        self.last_cleanup_status = "not_needed"
        self.base_env = self._validate_environment(
            base_env if base_env is not None else self._default_environment()
        )

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path,
        timeout_seconds: int = 60,
    ) -> ProcessResult:
        """Run synchronously; raise ValueError for invalid inputs or ProcessStartError.

        Execution and output collection share the timeout. Cleanup may take up
        to 3 additional seconds. Cancellation is propagated after cleanup.
        """
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence) or not command:
            raise ValueError("command must be a non-empty sequence of strings")
        command = list(command)
        if any(not isinstance(arg, str) or "\x00" in arg for arg in command) or not command[0]:
            raise ValueError("command requires a non-empty executable and strings without NUL")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        if not isinstance(cwd, (str, Path)) or not str(cwd).strip() or "\x00" in str(cwd):
            raise ValueError("cwd must be a non-empty path without NUL")
        self.last_cleanup_status = "not_needed"
        stdout_capture = _BoundedCapture(self.max_output_bytes)
        stderr_capture = _BoundedCapture(self.max_output_bytes)
        start_time = time.monotonic()
        try:
            popen_kwargs: dict[str, Any] = {
                "cwd": str(cwd),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "stdin": subprocess.DEVNULL,
                "env": self.base_env,
                "shell": False,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(command, **popen_kwargs)
        except (OSError, RuntimeError) as error:
            raise ProcessStartError(error) from error

        streams = {process.stdout: stdout_capture, process.stderr: stderr_capture}
        cleanup_error = None
        diagnostics = []
        supervisor = None
        self.last_cleanup_status = "unknown"
        try:
            if self.supervise_tree:
                from .process_supervisor import ProcessSupervisor

                supervisor = ProcessSupervisor(process)
            if os.name == "posix":
                for stream in streams:
                    os.set_blocking(stream.fileno(), False)
            timed_out = not self._collect_output(
                process, streams, start_time + timeout_seconds, supervisor
            )
            if timed_out or supervisor is not None:
                if supervisor is not None:
                    cleanup_error = supervisor.cleanup(lambda: self._drain_output(streams))
                    diagnostics = supervisor.diagnostics
                else:
                    cleanup_error = self._terminate_process_tree(process, diagnostics)
                # An escaped descendant may still hold a pipe. Drain only for a
                # bounded interval, then close our ends without waiting for EOF.
                if not self._collect_output(process, streams, time.monotonic() + 0.25):
                    cleanup_error = cleanup_error or (
                        "Output pipes remained open after termination; "
                        "descendant processes may still be running."
                    )
        except BaseException:
            # Includes user cancellation; re-raise after terminating owned work.
            if supervisor is not None:
                error = supervisor.cleanup(lambda: self._drain_output(streams))
                deadline = time.monotonic() + 0.25
                while not error and streams and time.monotonic() < deadline:
                    self._drain_output(streams)
                    if streams:
                        time.sleep(0.01)
                if streams:
                    error = error or "Output pipes remained open after cancellation."
                self.last_cleanup_status = "unknown" if error else "confirmed"
            else:
                error = self._terminate_process_tree(process, diagnostics)
                self.last_cleanup_status = "unknown" if error else "confirmed"
            raise
        finally:
            for stream in (process.stdout, process.stderr):
                stream.close()
        duration_ms = round((time.monotonic() - start_time) * 1000)
        self.last_cleanup_status = (
            "unknown" if cleanup_error else "confirmed" if timed_out or supervisor else "not_needed"
        )
        return ProcessResult(
            exit_code=None if timed_out else process.returncode,
            stdout=stdout_capture.text(),
            stderr=stderr_capture.text(),
            timed_out=timed_out,
            cleanup_error=cleanup_error,
            duration_ms=duration_ms,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            status="timed_out" if timed_out else "completed",
            cleanup_status=self.last_cleanup_status,
            output_complete=not (
                timed_out or cleanup_error or stdout_capture.truncated or stderr_capture.truncated
            ),
            pid=process.pid,
            process_group_id=process.pid if os.name == "posix" else None,
            cleanup_diagnostics=diagnostics,
        )

    @classmethod
    def _collect_output(cls, process, streams, deadline: float, supervisor=None) -> bool:
        """Poll both pipes and the leader under one deadline, without reader threads."""
        while streams or process.poll() is None:
            if supervisor is not None:
                supervisor.refresh()
            if time.monotonic() >= deadline:
                return False
            progressed = False
            for stream, capture in list(streams.items()):
                chunk = cls._read_available(stream)
                if chunk is None:
                    continue
                progressed = True
                if chunk:
                    capture.feed(chunk)
                else:
                    del streams[stream]
                    stream.close()
            if not progressed:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        return True

    @classmethod
    def _drain_output(cls, streams):
        """One bounded, nonblocking pass while the supervisor waits for exit."""
        for stream, capture in list(streams.items()):
            chunk = cls._read_available(stream)
            if chunk:
                capture.feed(chunk)
            elif chunk == b"":
                del streams[stream]
                stream.close()

    @staticmethod
    def _read_available(stream) -> bytes | None:
        """Return available bytes, b'' for EOF, or None when a pipe would block."""
        if os.name == "nt":
            # Windows anonymous pipes cannot use select(); PeekNamedPipe also
            # works on Python 3.11, which lacks nonblocking Windows pipe support.
            import ctypes
            import msvcrt
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            peek = kernel32.PeekNamedPipe
            peek.argtypes = [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ]
            peek.restype = wintypes.BOOL
            available = wintypes.DWORD()
            if not peek(
                msvcrt.get_osfhandle(stream.fileno()),
                None,
                0,
                None,
                ctypes.byref(available),
                None,
            ):
                error = ctypes.get_last_error()
                if error in (109, 232):  # Broken/disconnected pipe.
                    return b""
                raise ctypes.WinError(error)
            if not available.value:
                return None
            return os.read(stream.fileno(), min(8192, available.value))
        try:
            return os.read(stream.fileno(), 8192)
        except BlockingIOError:
            return None

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen,
        diagnostics: list | None = None,
    ) -> str | None:
        cleanup_error = None
        if os.name == "posix":
            diagnostics = diagnostics if diagnostics is not None else []

            def signal_group(sig: int, stage: str) -> str:
                nonlocal cleanup_error
                try:
                    os.killpg(process.pid, sig)
                    outcome, error_number = "sent" if sig else "exists", None
                except ProcessLookupError as error:
                    outcome, error_number = "gone", error.errno
                except OSError as error:
                    outcome, error_number = "unknown", error.errno
                    cleanup_error = (
                        f"Process-group cleanup unconfirmed: stage={stage}, "
                        f"pid={process.pid}, pgid={process.pid}, signal={sig}, errno={error.errno}."
                    )
                if len(diagnostics) < 64:
                    diagnostics.append({"stage": stage, "pid": process.pid,
                                        "pgid": process.pid, "signal": sig,
                                        "errno": error_number, "outcome": outcome})
                return outcome

            state = signal_group(signal.SIGTERM, "terminate")
            if state != "gone":
                deadline = time.monotonic() + 0.7
                while state != "unknown" and time.monotonic() < deadline:
                    process.poll()
                    state = signal_group(0, "probe_after_term")
                    if state == "gone":
                        break
                    time.sleep(0.02)
                if state != "gone":
                    signal_group(signal.SIGKILL, "kill")
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except PermissionError as error:
                    cleanup_error = f"Leader termination denied: pid={process.pid}, errno={error.errno}."
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                cleanup_error = "Command did not exit after process-group termination."
            deadline = time.monotonic() + 0.25
            while True:
                state = signal_group(0, "verify")
                if state in {"gone", "unknown"} or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            if state == "gone" and process.poll() is not None:
                cleanup_error = None
            elif not cleanup_error:
                cleanup_error = "Process group still exists after termination; cleanup is unconfirmed."
            if cleanup_error:
                logger.warning(cleanup_error)
            return cleanup_error
        # Best-effort fallback on platforms where killing an entire
        # descendant tree requires platform-specific facilities.
        if process.poll() is not None:
            return "Descendant process termination is not guaranteed on this platform."
        process.terminate()
        try:
            process.wait(timeout=1)
            return "Descendant process termination is not guaranteed on this platform."
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                return "Command did not exit after termination."
        return "Descendant process termination is not guaranteed on this platform."

    @staticmethod
    def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(environment, Mapping):
            raise ValueError("base_env must be a mapping of strings to strings")
        snapshot = dict(environment)
        for key, value in snapshot.items():
            if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
                raise ValueError("base_env keys must be non-empty strings without '=' or NUL")
            if not isinstance(value, str) or "\x00" in value:
                raise ValueError("base_env values must be strings without NUL")
            try:
                os.fsencode(key)
                os.fsencode(value)
            except UnicodeEncodeError:
                raise ValueError("base_env contains text unsupported by the OS encoding") from None
        return snapshot

    @staticmethod
    def _default_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "TMP",
            "TEMP",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "CUDA_HOME",
            "CUDA_PATH",
            "CUDA_VISIBLE_DEVICES",
            "LD_LIBRARY_PATH",
            "TORCH_CUDA_ARCH_LIST",
            "TORCH_EXTENSIONS_DIR",
            "TORCHINDUCTOR_CACHE_DIR",
            "TRITON_CACHE_DIR",
            "CUDA_CACHE_PATH",
            "XDG_CACHE_HOME",
            "MAX_JOBS",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
        }
        return {key: value for key, value in os.environ.items() if key in allowed}
