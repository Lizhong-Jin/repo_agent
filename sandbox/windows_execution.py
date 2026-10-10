"""Windows execution mechanics; workspace authorization lives in windows_native.

The trusted Windows backend implements the two policy hooks below. No POSIX hooks,
shell aliases, ambient environment or weaker process runner are used.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from host_support.windows_processes import WindowsCleanupError, WindowsIsolation

from .native_execution import NativeCleanupError
from .windows_git import GitQueryProcess


@dataclass(frozen=True)
class WindowsLaunch:
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    git_query: bool = False
    private_workspace: Path | None = None


class WindowsNativeExecutionAdapter:
    process_cleanup = "windows_job_kill_on_close_and_active_process_verification"

    def select_project_python(self, backend, explicit):
        # Selection must include Windows interpreter/read-access authorization.
        # Reusing POSIX directory discovery here would silently broaden access.
        return backend._select_windows_project_python(explicit)

    @contextmanager
    def prepare(self, backend, call):
        try:
            options = {}
            if hasattr(backend, "windows_state_root"):
                options["recovery_root"] = backend.windows_state_root
            with WindowsIsolation(**options) as isolation:
                # This hook must itself be a context manager: partial ACL/resource
                # acquisition is unwound on preparation, launch and cleanup errors.
                with backend._prepare_windows_call(isolation, call) as launch:
                    process_options = {"git_query": True} if launch.git_query else {}
                    process = isolation.process(
                        launch.command,
                        cwd=launch.cwd,
                        environment=launch.environment,
                        max_output_bytes=call.max_output_bytes,
                        **process_options,
                    )
                    yield (
                        GitQueryProcess(process, call, launch.private_workspace, backend.workspace)
                        if launch.git_query
                        else process
                    )
        except WindowsCleanupError as error:
            raise NativeCleanupError(str(error)) from error
