"""
Command tool policy and schema, backed by the reusable ProcessRunner.

The workspace check applies to the initial working directory, not to filesystem
access performed by the command. This tool is not an OS sandbox.
"""

import json
import math
import os
import platform
import shutil
import sys
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from llm import ToolDefinition

from ._internal.base import ExecutionKind, ToolEffects, ToolResult
from ._internal.errors import ToolErrorCode, tool_error
from ._internal.file_policy import is_credential_path
from ._internal.process_runner import ProcessRunner, ProcessStartError
from .scheduling import SERIAL


class _ProbeCleanupError(RuntimeError):
    """Stop probing if a subprocess could not be cleaned up."""


class GetExecutionEnvironmentTool:
    """Describe the current executor; application policy is constructor-only.

    This tool never establishes isolation. Only an isolated caller may enable
    subprocess probes; local mode reports basic facts without running commands.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    SECTIONS = ("execution", "system", "runtimes", "gpu")
    RUNTIME_COMMANDS = {
        "node": ("node", "--version"),
        "npm": ("npm", "--version"),
        "git": ("git", "--version"),
        "go": ("go", "version"),
        "gcc": ("gcc", "--version"),
        "clang": ("clang", "--version"),
        "cmake": ("cmake", "--version"),
        "ninja": ("ninja", "--version"),
    }

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        execution_context: Mapping[str, Any] | None = None,
        workspace_kind: str = "direct",
        command_timeout_seconds: int = 120,
        python_timeout_seconds: int = 30,
        probe_timeout_seconds: int = 45,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        if execution_context is not None and not isinstance(execution_context, Mapping):
            raise ValueError("execution_context must be a mapping")
        if execution_context and not execution_allowed:
            raise ValueError("execution_context requires an isolated execution caller")
        for name, value in (
            ("command_timeout_seconds", command_timeout_seconds),
            ("python_timeout_seconds", python_timeout_seconds),
            ("probe_timeout_seconds", probe_timeout_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.execution_allowed = execution_allowed
        self.execution_context = deepcopy(dict(execution_context or {}))
        if workspace_kind not in {"direct", "worktree"}:
            raise ValueError("workspace_kind must be direct or worktree")
        self.workspace_kind = workspace_kind
        self.command_timeout_seconds = command_timeout_seconds
        self.python_timeout_seconds = python_timeout_seconds
        self.probe_timeout_seconds = probe_timeout_seconds
        self.runner = ProcessRunner(max_output_bytes=32 * 1024)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_execution_environment",
            description=(
                "Inspect the environment where tools actually execute, not the CLI host. "
                "By default return execution policy, system facts and runtime/tool versions. "
                "Request the gpu section for Metal/MPS, PyTorch, Triton and CUDA probes. "
                "Component status is available, missing, unavailable or unknown; missing optional "
                "components are normal report data. Local mode never launches probe processes. "
                "Resource limits come from the executor policy, not host-wide CPU/memory totals. "
                "No environment-variable dump, installation or network access is performed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(self.SECTIONS)},
                        "minItems": 1,
                        "maxItems": len(self.SECTIONS),
                        "uniqueItems": True,
                        "default": list(self.SECTIONS[:3]),
                    },
                },
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        sections = arguments.get("sections", list(self.SECTIONS[:3]))
        if (
            set(arguments) - {"sections"}
            or not isinstance(sections, list)
            or not 1 <= len(sections) <= len(self.SECTIONS)
            or any(not isinstance(s, str) or s not in self.SECTIONS for s in sections)
            or len(set(sections)) != len(sections)
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "sections must be a non-empty unique array of execution, system, runtimes, gpu.",
            )
        data = {}
        deadline = time.monotonic() + self.probe_timeout_seconds
        try:
            # Fixed order keeps the report and shared probe budget deterministic.
            if "execution" in sections:
                data["execution"] = self._execution()
            if "system" in sections:
                data["system"] = {
                    "os": platform.system(),
                    "architecture": platform.machine(),
                    "python_version": platform.python_version(),
                }
            if "runtimes" in sections:
                data["runtimes"] = self._runtimes(deadline)
            if "gpu" in sections:
                data["gpu"] = self._gpu(deadline)
        except _ProbeCleanupError:
            data["cleanup_error"] = "Environment probe process cleanup could not be confirmed."
            return ToolResult(
                False,
                data,
                "PROBE_CLEANUP_FAILED",
                data["cleanup_error"],
                ToolEffects.process(data),
            )
        return ToolResult(True, data, effects=ToolEffects.process(data))

    def _execution(self) -> dict[str, Any]:
        allowed = self.execution_allowed
        bash = _bash_executable() if allowed else None
        report = {
            "mode": "isolated" if allowed else "local",
            "network": "unknown" if allowed else "not_exposed_by_execution_tools",
            "writable_paths": None if allowed else [str(self.workspace_root)],
            "file_tools_access_policy": "workspace_only_excluding_protected_paths",
            "changes_apply_to": "independent_workspace"
            if self.workspace_kind == "worktree"
            else "unknown"
            if allowed
            else "original_project",
            "workspace_kind": self.workspace_kind,
            "writeback_mode": "unknown" if allowed else "direct",
            "resources": None,
            "persistence": None,
        }
        report.update(deepcopy(self.execution_context))
        report.update(
            {
                "workspace_root": str(self.workspace_root),
                "working_directory": str(self.workspace_root),
                "command_execution_allowed": allowed,
                "python_execution_allowed": allowed,
                "shell_execution_allowed": bool(bash),
                "shell": {
                    "dialect": "bash",
                    "executable": bash,
                    "status": "available" if bash else "unavailable",
                    "reason": (
                        None
                        if bash
                        else "execution_disabled"
                        if not allowed
                        else "unsupported_platform"
                        if sys.platform not in {"linux", "darwin"}
                        else "bash_missing"
                    ),
                },
                "tool_limits": {
                    "command_default_timeout_seconds": (
                        min(60, self.command_timeout_seconds) if allowed else None
                    ),
                    "command_max_timeout_seconds": (
                        self.command_timeout_seconds if allowed else None
                    ),
                    "python_default_timeout_seconds": (
                        min(10, self.python_timeout_seconds) if allowed else None
                    ),
                    "python_max_timeout_seconds": self.python_timeout_seconds if allowed else None,
                    "command_output_bytes_per_stream": 32 * 1024 if allowed else None,
                    "environment_probe_timeout_seconds": (
                        self.probe_timeout_seconds if allowed else None
                    ),
                },
            }
        )
        return report

    def _probe(self, command: list[str], deadline: float, timeout: int = 3) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "unknown", "reason": "probe_budget_exhausted"}
        try:
            result = self.runner.run(
                command,
                cwd=self.workspace_root,
                timeout_seconds=min(timeout, max(1, math.ceil(remaining))),
            )
        except ProcessStartError as error:
            return {
                "status": (
                    "missing" if isinstance(error.cause, FileNotFoundError) else "unavailable"
                ),
                "reason": type(error.cause).__name__,
            }
        if result.cleanup_error:
            raise _ProbeCleanupError
        if result.timed_out:
            return {"status": "unknown", "reason": "probe_timed_out"}
        if result.exit_code != 0:
            return {
                "status": "unavailable",
                "reason": "probe_failed",
                "exit_code": result.exit_code,
            }
        if result.stdout_truncated or result.stderr_truncated:
            return {"status": "unknown", "reason": "probe_output_truncated"}
        return {"status": "available", "output": result.stdout.strip()}

    def _runtimes(self, deadline: float) -> dict:
        report = {
            "python": {
                "status": "available",
                "version": platform.python_version(),
                "executable": sys.executable,
            },
        }
        environments = self.execution_context.get("python_environments", {})
        if environments:
            report["agent_python"] = dict(report["python"])
            report["python"] = {
                **environments.get("project_info", {}),
                "executable": environments["project"],
                "source": environments.get("source"),
                "status": "available" if environments.get("project_info") else "unknown",
            }
        for name, argv in self.RUNTIME_COMMANDS.items():
            if not self.execution_allowed:
                report[name] = {"status": "unknown", "reason": "local_probes_disabled"}
                continue
            executable = shutil.which(argv[0], path=self.runner.base_env.get("PATH", ""))
            if executable is None:
                report[name] = {"status": "missing"}
                continue
            result = self._probe([executable, *argv[1:]], deadline)
            if result["status"] == "available":
                output = result.pop("output")
                if output:
                    result["version"] = output.splitlines()[0][:512]
                else:
                    result = {"status": "unknown", "reason": "empty_version_output"}
            report[name] = result
        return report

    def _gpu(self, deadline: float) -> dict:
        if not self.execution_allowed:
            return {"status": "unknown", "reason": "local_probes_disabled"}
        # -I avoids importing workspace modules named torch, triton or json.
        # The installed probe script is shared with the existing GPU smoke tools.
        script = Path(__file__).resolve().parents[1] / "sandbox" / "compute_probe.py"
        python = self.execution_context.get("python_environments", {}).get(
            "project", sys.executable
        )
        result = self._probe([python, "-I", str(script)], deadline, timeout=30)
        macos_native = self.execution_context.get("platform") == "macos"
        if result["status"] != "available":
            if macos_native:
                return self._metal_gpu_report({"framework_probe": result})
            return result
        try:
            report = json.loads(result["output"])
            if (
                not isinstance(report, dict)
                or type(report.get("cuda_available")) is not bool
                or not isinstance(report.get("devices"), list)
                or not isinstance(report.get("errors"), dict)
            ):
                raise ValueError("Invalid compute probe report")
        except (ValueError, TypeError):
            if macos_native:
                return self._metal_gpu_report(
                    {
                        "framework_probe": {
                            "status": "unknown",
                            "reason": "invalid_probe_response",
                        }
                    }
                )
            return {"status": "unknown", "reason": "invalid_probe_response"}
        if macos_native:
            return self._metal_gpu_report(report)
        # A successful probe is not evidence that CUDA is available.
        report["status"] = (
            "unknown"
            if not report.get("torch") or "torch" in report["errors"]
            else "available"
            if report["cuda_available"] and report["devices"]
            else "unavailable"
        )
        executable = shutil.which("nvidia-smi", path=self.runner.base_env.get("PATH", ""))
        report["driver"] = (
            self._probe(
                [executable, "--query-gpu=name,driver_version", "--format=csv,noheader"], deadline
            )
            if executable
            else {"status": "missing"}
        )
        return report

    def _metal_gpu_report(self, report):
        access = self.execution_context.get("gpu_access", {})
        probe = access.get("startup_probe") or {}
        enabled = access.get("enabled") is True and probe.get("metal_kernel_verified") is True
        metal = {
            "status": "available" if enabled else "unavailable",
            "kernel_verified": enabled,
            "device": probe.get("device"),
            "verification": "native_startup_probe",
        }
        if not enabled:
            metal["reason"] = access.get("disabled_reason") or "metal_not_enabled"
        return {
            **report,
            "status": metal["status"],
            "backend": "metal",
            "metal": metal,
            "mps": report.get("mps", {"status": "unknown", "kernel_verified": False}),
            "driver": {
                "status": "available" if enabled else "unknown",
                "source": "macOS system Metal driver",
            },
        }


# These tools share validation-result and writeback semantics. Keep host-side
# proxy handling in sync through this one trusted catalog, never model input.
PROCESS_EXECUTION_TOOLS = frozenset({"run_command", "run_python", "run_shell"})


def _bash_executable() -> str | None:
    """Resolve system Bash in the executor, never through the workspace PATH."""
    if sys.platform not in {"linux", "darwin"}:
        return None
    for path in ("/bin/bash", "/usr/bin/bash"):
        if Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return None


class _ProcessTool:
    """Common cwd/timeout validation, launch errors and bounded process results."""

    def _run(self, command, arguments) -> ToolResult:
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "cwd must be a non-empty string without NUL characters.",
            )
        timeout_seconds = arguments.get("timeout_seconds", self.default_timeout_seconds)
        if (
            type(timeout_seconds) is not int
            or timeout_seconds <= 0
            or timeout_seconds > self.max_timeout_seconds
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "timeout_seconds must be a positive integer not exceeding "
                f"{self.max_timeout_seconds}.",
            )

        try:
            target_cwd = (self.workspace_root / cwd).resolve()
            if is_credential_path(self.workspace_root / cwd, target_cwd):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target_cwd.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target_cwd.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY, f"cwd '{cwd}' is not a directory.")
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND, f"cwd '{cwd}' does not exist.")
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR, "cwd does not exist.")

        try:
            result = self.runner.run(command, cwd=target_cwd, timeout_seconds=timeout_seconds)
        except ProcessStartError as error:
            if isinstance(error.cause, FileNotFoundError):
                return tool_error(
                    ToolErrorCode.FILE_NOT_FOUND, "Unable to find the requested executable."
                )
            if isinstance(error.cause, PermissionError):
                return tool_error(
                    ToolErrorCode.PERMISSION_DENIED,
                    "The command cannot be executed with current permissions.",
                )
            return tool_error("PROCESS_START_ERROR", "Unable to start the command.")

        return ToolResult(
            True,
            {
                "cwd": target_cwd.relative_to(self.workspace_root).as_posix(),
                **asdict(result),
            },
            effects=ToolEffects.process(asdict(result)),
        )


# RunCommandTool
class RunCommandTool(_ProcessTool):
    """Run bounded, non-interactive subprocesses and capture their output."""

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        default_timeout_seconds: int = 60,
        max_timeout_seconds: int = 120,
        max_output_bytes: int = 32 * 1024,
        max_command_args: int = 128,
        max_argument_chars: int = 4096,
        base_env: Mapping[str, str] | None = None,
    ) -> None:

        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        self.execution_allowed = execution_allowed
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("default_timeout_seconds", default_timeout_seconds),
            ("max_timeout_seconds", max_timeout_seconds),
            ("max_output_bytes", max_output_bytes),
            ("max_command_args", max_command_args),
            ("max_argument_chars", max_argument_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if default_timeout_seconds > max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_command_args = max_command_args
        self.max_argument_chars = max_argument_chars
        self.runner = ProcessRunner(max_output_bytes=max_output_bytes, base_env=base_env)
        self.base_env = self.runner.base_env

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_command",
            description=(
                "Run a non-interactive command in a workspace directory. "
                "Pass the command as an argv array; shell syntax, pipes, "
                "redirection, and command chaining are not interpreted. "
                "Use run_shell for Bash syntax on supported executors. "
                "On Windows, shell built-ins require an explicit interpreter; .bat/.cmd files "
                "may be interpreted by the OS shell, so argv does not guarantee literal arguments. "
                "stdout and stderr are captured with bounded output. "
                "A non-zero exit code is returned as a normal command result. "
                "To verify an expected failure (such as invalid input), use run_python "
                "with subprocess.run and assert the expected exit code, so the "
                "verification itself exits zero when it passes. "
                f"The command and output collection share a timeout of at most "
                f"{self.max_timeout_seconds} seconds. Termination and final output cleanup "
                "may take up to 3 additional seconds. cleanup_error reports incomplete "
                "or unconfirmed cleanup; Windows descendant termination is best-effort. "
                "Timeouts retain collected stdout/stderr with status=timed_out and "
                "output_complete=false. cleanup_status is separate from execution status. "
                "Output activity does not extend the total timeout."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "minItems": 1,
                        "maxItems": self.max_command_args,
                        "description": ("Executable and arguments as an argv array."),
                    },
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": ("Workspace-relative working directory."),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_timeout_seconds,
                        "default": self.default_timeout_seconds,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if not self.execution_allowed:
            return tool_error(
                "SANDBOX_REQUIRED",
                "Command execution requires Docker isolation. Use --sandbox docker.",
            )
        if set(arguments.keys()) - {"command", "cwd", "timeout_seconds"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: command, cwd, timeout_seconds.",
            )
        command = arguments.get("command")
        if not isinstance(command, list) or not command:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "command must be a non-empty array of strings.",
            )
        if len(command) > self.max_command_args:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"command exceeds maximum length of {self.max_command_args} arguments.",
            )
        if any(
            not isinstance(arg, str) or "\x00" in arg or len(arg) > self.max_argument_chars
            for arg in command
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Each argument must be a string with a maximum length of "
                f"{self.max_argument_chars} characters.",
            )
        if not command[0]:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "First argument must be a non-empty string."
            )
        result = self._run(command, arguments)
        return (
            replace(result, data={"command": command, **result.data}) if result.success else result
        )


# RunPythonTool
class RunPythonTool(_ProcessTool):
    """Run bounded Python snippets in a separate subprocess."""

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        python_executable: str | Path | None = None,
        default_timeout_seconds: int = 10,
        max_timeout_seconds: int = 30,
        max_output_bytes: int = 32 * 1024,
        max_code_bytes: int = 128 * 1024,
        base_env: Mapping[str, str] | None = None,
    ) -> None:

        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        self.execution_allowed = execution_allowed
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("default_timeout_seconds", default_timeout_seconds),
            ("max_timeout_seconds", max_timeout_seconds),
            ("max_output_bytes", max_output_bytes),
            ("max_code_bytes", max_code_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if default_timeout_seconds > max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        self.python_executable = str(
            python_executable if python_executable is not None else sys.executable
        )
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds
        self.max_code_bytes = max_code_bytes
        self.runner = ProcessRunner(max_output_bytes=max_output_bytes, base_env=base_env)
        self.base_env = self.runner.base_env

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_python",
            description=(
                "Execute a Python snippet in a separate non-interactive "
                "subprocess inside the workspace. Each call is an independent process; "
                "Python variables and imported modules do not persist across calls. "
                "Intended for small calculations, parsing, experiments, and debugging. "
                "Use run_command for project scripts, tests, builds, or longer-running programs. "
                f"Execution is limited to {self.max_timeout_seconds} seconds. "
                "Timeouts retain collected stdout/stderr with status=timed_out and "
                "output_complete=false; cleanup_status reports cleanup separately. "
                "Output activity does not extend the total timeout."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "description": ("Python source code to execute."),
                    },
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": ("Workspace-relative working directory."),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_timeout_seconds,
                        "default": self.default_timeout_seconds,
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if not self.execution_allowed:
            return tool_error(
                "SANDBOX_REQUIRED",
                "Command execution requires Docker isolation. Use --sandbox docker.",
            )
        if set(arguments.keys()) - {"code", "cwd", "timeout_seconds"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: code, cwd, timeout_seconds.",
            )
        code = arguments.get("code")
        if not isinstance(code, str) or not code:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "code must be a non-empty strings.",
            )
        try:
            if len(code.encode("utf-8")) > self.max_code_bytes:
                return tool_error(
                    "CODE_TOO_LARGE",
                    f"Python code exceeds {self.max_code_bytes} UTF-8 bytes.",
                )
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        if "\x00" in code:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "code must not contain NUL characters.",
            )
        return self._run([self.python_executable, "-u", "-c", code], arguments)


class RunShellTool(RunCommandTool):
    """A Bash frontend to the same bounded process execution contract."""

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        default_timeout_seconds: int = 60,
        max_timeout_seconds: int = 120,
        max_output_bytes: int = 32 * 1024,
        max_script_bytes: int = 64 * 1024,
        base_env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            default_timeout_seconds=default_timeout_seconds,
            max_timeout_seconds=max_timeout_seconds,
            max_output_bytes=max_output_bytes,
            base_env=base_env,
        )
        if type(max_script_bytes) is not int or max_script_bytes <= 0:
            raise ValueError("max_script_bytes must be a positive integer")
        self.max_script_bytes = max_script_bytes
        # Native replaces the runner with its already-supervised sandbox adapter.
        # Docker/direct isolated callers also clean up observed background children.
        self.runner.supervise_tree = True
        for name in list(self.base_env):
            if name in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"} or name.startswith(
                "BASH_FUNC_"
            ):
                self.base_env.pop(name)

    @property
    def definition(self) -> ToolDefinition:
        parameters = deepcopy(super().definition.parameters)
        del parameters["properties"]["command"]
        parameters["properties"]["script"] = {
            "type": "string",
            "minLength": 1,
            "description": f"Bash source, at most {self.max_script_bytes} UTF-8 bytes.",
        }
        parameters["required"] = ["script"]
        return ToolDefinition(
            name="run_shell",
            description=(
                "Run a non-interactive Bash script for pipelines, redirection, loops or multiline "
                "commands. Use run_command for a single executable with literal argv arguments. "
                "Each call is independent: cwd, variables and functions do not persist. "
                "Uses system Bash on Linux/macOS, including Linux Docker on a Windows host. "
                "Direct Windows execution is unsupported; never converts to PowerShell or cmd. "
                "No login/profile/startup scripts or inherited Bash functions are loaded. "
                "pipefail is enabled, but errexit and nounset are not: use && or explicit checks "
                "when later steps depend on earlier success. The exit code is the script's final "
                "status, not proof that every command succeeded. Do not launch detached services. "
                "Timeout covers execution and output collection, at most "
                f"{self.max_timeout_seconds} seconds, plus bounded cleanup. "
                "Output does not extend the deadline. "
                "Nonzero exits are normal results. Timeouts retain bounded stdout/stderr with "
                "status=timed_out and output_complete=false; "
                "cleanup_status reports cleanup separately."
            ),
            parameters=parameters,
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if not self.execution_allowed:
            return tool_error("SANDBOX_REQUIRED", "Shell execution requires an isolated executor.")
        if set(arguments) - {"script", "cwd", "timeout_seconds"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "Allowed arguments: script, cwd, timeout_seconds."
            )
        script = arguments.get("script")
        if not isinstance(script, str) or not script.strip() or "\x00" in script:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "script must be non-empty Bash source without NUL characters.",
            )
        try:
            if len(script.encode("utf-8")) > self.max_script_bytes:
                return tool_error(
                    "SCRIPT_TOO_LARGE", f"Shell script exceeds {self.max_script_bytes} UTF-8 bytes."
                )
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        # Check at execution time, not registration: Windows hosts build Linux
        # Docker tool schemas locally, but the worker selects the actual shell.
        if sys.platform not in {"linux", "darwin"}:
            return tool_error(
                "SHELL_UNSUPPORTED_PLATFORM",
                "run_shell requires Linux/macOS Bash. On Windows use Linux Docker "
                "or run_command with an explicitly chosen Windows executable.",
            )
        executable = _bash_executable()
        if executable is None:
            return tool_error("SHELL_UNAVAILABLE", "System Bash was not found in /bin or /usr/bin.")
        # -p also suppresses BASH_ENV/functions/options if a sandbox adapter
        # supplies its own environment. This flag does not establish isolation.
        command = [
            executable,
            "--noprofile",
            "--norc",
            "-p",
            "-o",
            "pipefail",
            "-c",
            script,
            "repo-agent-shell",
        ]
        result = self._run(command, arguments)
        return (
            replace(result, data={"shell": "bash", "shell_executable": executable, **result.data})
            if result.success
            else result
        )
