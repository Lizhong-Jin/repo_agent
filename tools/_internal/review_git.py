"""Pin host Git and forbid implicit downloads for read-only review queries."""

import os
import shutil
from pathlib import Path

from host_support.paths import find_windows_executable
from host_support.processes import ProcessRunner, ProcessStartError


class ReviewGitRunner:
    def __init__(self, runner, root):
        self.root = Path(root).resolve()
        directories = []
        for entry in runner.base_env.get("PATH", "").split(os.pathsep):
            path = Path(entry.strip('"'))
            if path.is_absolute() and not path.resolve().is_relative_to(self.root):
                directories.append(str(path))
        self.runner = ProcessRunner(
            max_output_bytes=runner.max_output_bytes,
            base_env={
                **runner.base_env,
                "PATH": os.pathsep.join(directories),
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_ALLOW_PROTOCOL": "",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )

    def run(self, command, *, cwd, timeout_seconds=30):
        if not command or command[0] != "git":
            raise ProcessStartError(PermissionError("Only audited Git queries are allowed"))
        executable = (
            find_windows_executable("git", exclude=(self.root,))
            if os.name == "nt"
            else shutil.which("git", path=self.runner.base_env.get("PATH", ""))
        )
        if not executable:
            raise ProcessStartError(FileNotFoundError("Git is unavailable"))
        executable = Path(executable).resolve()
        if executable.is_relative_to(self.root):
            raise ProcessStartError(PermissionError("Project executables cannot provide Git"))
        prefix = [
            str(executable),
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-c",
            "core.untrackedCache=false",
        ]
        # Older Git versions need this explicit guard in addition to NO_LAZY_FETCH.
        result = self.runner.run(
            [
                *prefix,
                "config",
                "--includes",
                "--null",
                "--get-regexp",
                r"^(extensions\.partialclone|remote\..*\.promisor)$",
            ],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        if (
            result.exit_code != 1
            or result.stdout
            or result.stderr
            or result.timed_out
            or result.cleanup_error
            or result.cleanup_status == "unknown"
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise ProcessStartError(PermissionError("Cannot establish offline Git query safety"))
        return self.runner.run([*prefix, *command[1:]], cwd=cwd, timeout_seconds=timeout_seconds)
