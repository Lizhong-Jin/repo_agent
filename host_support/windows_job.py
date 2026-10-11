"""Job lifetime management for trusted Windows host clients, not isolation.

The child joins a kill-on-close Job atomically at creation, before any code
runs. Children cannot opt out. Container lifetime is managed separately.
"""

import os
import time

from .windows_isolation import WindowsIsolationAPI


def _pipe_stream(handle, release):
    import msvcrt

    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    release(handle)
    try:
        return os.fdopen(descriptor, "rb", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


class WindowsJobProcess:
    """Popen-shaped process and supervisor used by ProcessRunner on Windows."""

    def __init__(self, command, *, cwd, environment, api=None):
        self.api = api if api is not None else WindowsIsolationAPI()
        self.handles = []
        self.job = None
        self.information = None
        self.stdout = self.stderr = None
        self.pid = None
        self.returncode = None
        self.closed = False
        self.diagnostics = []
        try:
            names = [name.upper() for name in environment]
            if len(names) != len(set(names)):
                raise ValueError("Windows environment has case-insensitive duplicate names")
            block = (
                "\x00".join(
                    f"{name}={environment[name]}" for name in sorted(environment, key=str.upper)
                )
                + "\x00\x00"
            )
            self.job = self.api.create_job(self._own)
            stdin_read, stdin_write = self.api.create_pipe(self._own, child_reads=True)
            stdout_read, stdout_write = self.api.create_pipe(self._own)
            stderr_read, stderr_write = self.api.create_pipe(self._own)
            self.information = self.api.create_trusted_suspended(
                self.job,
                (stdin_read, stdout_write, stderr_write),
                command,
                cwd,
                block,
                self._own,
            )
            self.pid = self.information.pid
            for handle in (stdin_read, stdin_write, stdout_write, stderr_write):
                self._close(handle)
            # open_osfhandle transfers handle ownership to the Python stream.
            self.stdout = _pipe_stream(stdout_read, self.handles.remove)
            self.stderr = _pipe_stream(stderr_read, self.handles.remove)
            self.api.resume(self.information.thread)
            self._close(self.information.thread)
        except BaseException:
            self.cleanup(lambda: None)
            for stream in (self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            raise

    def _own(self, handle):
        self.handles.append(handle)
        return handle

    def _close(self, handle):
        self.api.close_handle(handle)
        self.handles.remove(handle)

    def poll(self):
        if not self.closed and self.information is not None and self.returncode is None:
            self.returncode = self.api.poll(self.information.process)
        return self.returncode

    def refresh(self):
        # Kernel Job membership replaces process-table sampling.
        pass

    def cleanup(self, drain):
        if self.closed:
            return self._cleanup_error()
        try:
            if self.job is not None:
                self.api.terminate_job(self.job)
                deadline = time.monotonic() + 2
                while self.api.active_processes(self.job):
                    if time.monotonic() >= deadline:
                        raise OSError("Windows client Job still has active processes")
                    drain()
                    time.sleep(0.01)
                # Job accounting can reach zero before the process handle is
                # signaled. Wait for both under the same cleanup deadline;
                # successful termination alone never proves clean exit.
                while self.information is not None and self.poll() is None:
                    if time.monotonic() >= deadline:
                        raise OSError("Windows client leader exit could not be confirmed")
                    drain()
                    time.sleep(0.01)
        except Exception as error:
            self.diagnostics.append({"stage": "job_cleanup", "error": str(error)})
        finally:
            # Job was acquired first and is closed last, even after startup
            # errors. Kill-on-close is a fallback, never proof of clean exit.
            for handle in list(reversed(self.handles)):
                try:
                    self._close(handle)
                except OSError as error:
                    self.diagnostics.append({"stage": "close_handle", "error": str(error)})
            self.closed = True
        return self._cleanup_error()

    def _cleanup_error(self):
        return "Windows client Job cleanup unconfirmed" if self.diagnostics else None
