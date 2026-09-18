"""
Command tool policy and schema, backed by the reusable ProcessRunner.

The workspace check applies to the initial working directory, not to filesystem
access performed by the command. This tool is not an OS sandbox.
"""

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any
import sys

from llm import ToolDefinition

from .base import ToolResult
from .errors import ToolErrorCode, tool_error
from .file_policy import is_credential_path
from .process_runner import ProcessRunner, ProcessStartError

# RunCommandTool
class RunCommandTool:
    """Run bounded, non-interactive subprocesses and capture their output."""

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
                "stdout and stderr are captured with bounded output. "
                "A non-zero exit code is returned as a normal command result. "
                "To verify an expected failure (such as invalid input), use run_python "
                "with subprocess.run and assert the expected exit code, so the "
                "verification itself exits zero when it passes. "
                f"The command and output collection share a timeout of at most "
                f"{self.max_timeout_seconds} seconds. Termination and final output cleanup "
                "may take up to 3 additional seconds. cleanup_error reports incomplete "
                "or unconfirmed cleanup; Windows descendant termination is best-effort."
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
            success=True,
            data={
                "command": command,
                "cwd": target_cwd.relative_to(self.workspace_root).as_posix(),
                **asdict(result),
            },
        )

# RunPythonTool
class RunPythonTool:
    """Run bounded Python snippets in a separate subprocess."""

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
        self.python_executable = str(python_executable if python_executable is not None else sys.executable)
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
                "subprocess inside the workspace. Each call is an independent process."
                "across calls. Intended for small calculations, parsing, experiments, and debugging. "
                "Use run_command for project scripts, tests, builds, or longer-running programs. "
                f"Execution is limited to {self.max_timeout_seconds} seconds."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Python source code to execute."
                        ),
                    },
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": (
                            "Workspace-relative working directory."
                        ),
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
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "code must be a non-empty strings.",)
        try:
            if len(code.encode("utf-8")) > self.max_code_bytes:
                return tool_error( "CODE_TOO_LARGE", f"Python code exceeds {self.max_code_bytes} UTF-8 bytes.",)
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        if "\x00" in code:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "code must not contain NUL characters.",) 
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
            result = self.runner.run(
                [self.python_executable, "-u", "-c", code],
                cwd=target_cwd,
                timeout_seconds=timeout_seconds,
            )
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
            success=True,
            data={
                "cwd": target_cwd.relative_to(self.workspace_root).as_posix(),
                **asdict(result),
            },
        )
