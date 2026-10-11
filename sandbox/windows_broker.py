"""Fixed host-side environment service; every executable probe stays in LPAC.

This is an application-owned service, not an RPC endpoint for arbitrary paths or
commands. The normal tool validates requested sections before calling any probe.
No workspace Python, configuration or third-party library is imported on host.
"""

import os
from dataclasses import replace
from pathlib import Path

from host_support.processes import ProcessStartError
from tools.execute import GetExecutionEnvironmentTool


class _EnvironmentRunner:
    def __init__(self, backend):
        self.backend = backend
        layout = backend._project_layout
        # Match the sources mapped into the private Python/System32 PATH, not
        # the host's ambient PATH (which can include unrelated installations).
        self.base_env = {
            "PATH": os.pathsep.join(
                map(str, (layout.executable.parent, layout.base, backend.system_directory))
            )
        }

    def run(self, command, *, cwd, timeout_seconds):
        if (
            not isinstance(command, (list, tuple))
            or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        ):
            raise ProcessStartError(PermissionError("Invalid environment probe"))
        if Path(cwd) != self.backend.workspace:
            raise ProcessStartError(PermissionError("Environment probe workspace mismatch"))
        if not self.backend.healthy:
            raise ProcessStartError(PermissionError("Executor cleanup is unconfirmed"))
        command = list(command)
        probe = Path(__file__).resolve().with_name("compute_probe.py")
        python = self.backend.project_python.executable
        if command == [str(python), "-I", str(probe)]:
            # Use the backend's frozen trusted source, never a workspace script.
            source = (self.backend.runtime / "sandbox" / "compute_probe.py").read_text(
                encoding="utf-8"
            )
            command = [str(python), "-I", "-c", source]
        elif not any(
            tuple(command[1:]) == tuple(argv[1:])
            and Path(command[0]).name.lower() in {argv[0], argv[0] + ".exe"}
            for argv in GetExecutionEnvironmentTool.RUNTIME_COMMANDS.values()
        ) and not (
            Path(command[0]).name.lower() in {"nvidia-smi", "nvidia-smi.exe"}
            and command[1:] == ["--query-gpu=name,driver_version", "--format=csv,noheader"]
        ):
            raise ProcessStartError(PermissionError("Unknown environment probe"))
        try:
            return self.backend._run(
                command,
                cwd=cwd,
                timeout=timeout_seconds,
                project=True,
                max_output_bytes=32 * 1024,
            )
        except (OSError, ValueError) as error:
            raise ProcessStartError(error) from error


class WindowsEnvironmentService(GetExecutionEnvironmentTool):
    """Report host-owned policy plus sandboxed runtime observations."""

    def __init__(self, backend):
        super().__init__(
            backend.workspace,
            execution_allowed=True,
            execution_context=backend.execution_context(),
            **backend._tool_limits(),
        )
        self.backend = backend
        self.runner = _EnvironmentRunner(backend)

    def _execution(self):
        result = super()._execution()
        result["working_directory"] = "per-call private workspace"
        result["environment_report_source"] = "host_policy_and_isolated_probes"
        return result

    def _find_executable(self, name):
        # shutil.which on Windows may search cwd or honor ambient PATHEXT,
        # even with an explicit PATH. Only mapped native EXEs are eligible.
        for directory in self.runner.base_env["PATH"].split(os.pathsep):
            candidate = Path(directory) / (name + ".exe")
            if candidate.is_file():
                return str(candidate)
        return None

    def execute(self, arguments):
        result = super().execute(arguments)
        return replace(result, data={**result.data, "execution_allowed": self.backend.healthy})
