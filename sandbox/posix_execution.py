"""Existing macOS/Linux native launch behavior behind the execution adapter."""

import json
import os
import shlex
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from host_support.processes import ProcessRunner

from .native_execution import NativeCleanupError
from .project_python import select_python


@contextmanager
def _call_directory(parent):
    temporary = tempfile.TemporaryDirectory(prefix="call-", dir=parent)
    try:
        yield Path(temporary.name)
    finally:
        try:
            temporary.cleanup()
        except OSError as error:
            raise NativeCleanupError("Native call directory cleanup failed") from error


@dataclass
class _PosixProcess:
    runner: ProcessRunner
    command: list[str]
    cwd: Path

    @property
    def last_cleanup_status(self):
        return self.runner.last_cleanup_status

    def run(self, *, timeout_seconds):
        return self.runner.run(self.command, cwd=self.cwd, timeout_seconds=timeout_seconds)


class PosixNativeExecutionAdapter:
    """Stateless adapter; policy generation stays in the concrete OS backend."""

    process_cleanup = "host_supervised_pid_and_start_time_best_effort"

    def select_project_python(self, backend, explicit):
        return select_python(
            backend.workspace,
            explicit,
            agent_python=backend.python,
            trusted_paths=backend.read_paths,
            environment={"PATH": os.devnull} if backend.isolated_workspace else None,
        )

    @contextmanager
    def prepare(self, backend, call):
        with _call_directory(backend.directory) as control:
            scratch = control / "scratch"
            scratch.mkdir()
            read_paths = backend.read_paths
            project_environment = getattr(backend, "project_python", None)
            if project_environment:
                # Project interpreters inside the workspace remain read-only,
                # including during calls that use only the trusted Agent Python.
                read_paths = tuple(
                    dict.fromkeys(
                        (
                            *read_paths,
                            *(
                                path
                                for path in project_environment.read_paths
                                if path.is_relative_to(backend.workspace)
                            ),
                        )
                    )
                )
            selected = project_environment if call.project else None
            if selected:
                read_paths = tuple(dict.fromkeys((*read_paths, *selected.read_paths)))
            aliases = None
            if selected and call.request is None:
                aliases = control / "python-bin"
                aliases.mkdir()
                for name in ("python", "python3"):
                    path = aliases / name
                    path.write_text(
                        "#!/bin/sh\nexec " + shlex.quote(str(selected.executable)) + ' "$@"\n'
                    )
                    path.chmod(0o555)
                read_paths = (*read_paths, aliases)
            command = call.command
            if call.request is not None:
                request_path = control / "request.json"
                request_path.write_text(json.dumps(call.request))
                read_paths = (*read_paths, request_path)
                bootstrap = (
                    "import sys; "
                    f"sys.path.insert(0, {str(backend.runtime)!r}); "
                    "from sandbox.worker import execute_request; import json; "
                    f"execute_request(json.load(open({str(request_path)!r})), "
                    f"{str(backend.workspace)!r})"
                )
                command = [str(backend.python), "-I", "-c", bootstrap]
            invocation = backend._sandbox_command(
                command, control, scratch, read_paths, git_read=call.git_read
            )
            environment = backend._environment(scratch)
            # Only project commands receive interpreter aliases and project PATH.
            # Workers retain trusted PATH even when granted project read access.
            if selected and call.request is None:
                environment["PATH"] = os.pathsep.join(
                    [str(aliases), str(selected.executable.parent), environment["PATH"]]
                )
                prefix = selected.executable.parent.parent
                if (prefix / "conda-meta").is_dir():
                    environment["CONDA_PREFIX"] = str(prefix)
                elif (prefix / "pyvenv.cfg").is_file():
                    environment["VIRTUAL_ENV"] = str(prefix)
            runner = ProcessRunner(
                max_output_bytes=call.max_output_bytes,
                base_env=environment,
                supervise_tree=True,
            )
            yield _PosixProcess(runner, invocation, call.cwd)
