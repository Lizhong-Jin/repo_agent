"""
Tools related to Git
"""

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm import ToolDefinition

from ._internal.base import ExecutionKind, ToolResult
from ._internal.errors import ToolErrorCode, tool_error
from ._internal.file_policy import is_credential_path
from ._internal.process_runner import ProcessRunner, ProcessStartError
from .scheduling import SERIAL


class _GitFilterPolicy:
    """Shared gate for Git commands that can inspect worktree content."""

    def _check_external_filters(self, repo_root: Path) -> ToolResult | None:
        """Fail closed before Git can normalize any worktree file.

        Check the effective configuration, including include/includeIf and worktree
        config. Refuse even currently unused drivers: attributes can select them
        without changing config. This is a preflight check, not an OS sandbox or
        an atomic guarantee against concurrent host edits to Git configuration.
        """
        if self.execution_allowed:
            return None
        result = self.runner.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "config",
                "--includes",
                "--null",
                "--get-regexp",
                r"^filter\..*\.(clean|process)$",
            ],
            cwd=repo_root,
            timeout_seconds=self.timeout_seconds,
        )
        if result.timed_out:
            return tool_error("GIT_TIMEOUT", "Git filter configuration check timed out.")
        if result.cleanup_error or result.stdout_truncated or result.stderr_truncated:
            return tool_error(
                "GIT_CONFIG_CHECK_FAILED", "Cannot fully verify Git filter configuration."
            )
        if result.exit_code == 1 and not result.stdout:
            return None  # git config reports no matching keys with exit status 1.
        if result.exit_code != 0:
            return tool_error("GIT_CONFIG_CHECK_FAILED", "Cannot verify Git filter configuration.")
        # --null encodes each entry as key LF value NUL; never expose commands.
        entries = result.stdout.split("\0")
        if not result.stdout.endswith("\0") or any("\n" not in item for item in entries[:-1]):
            return tool_error(
                "GIT_CONFIG_CHECK_FAILED", "Invalid Git filter configuration response."
            )
        # Do not strip: Unicode whitespace can still be a shell command name.
        if any(item.partition("\n")[2] for item in entries[:-1]):
            return tool_error(
                "GIT_EXTERNAL_FILTER_REQUIRES_SANDBOX",
                "Repository clean/process filters may execute commands. "
                "Local Git tools refuse this configuration; use native or Docker isolation.",
            )
        return None


# GitDiffTool
class GitDiffTool(_GitFilterPolicy):
    """Show bounded Git diffs inside a workspace repository."""

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_paths: int = 64,
        max_context_lines: int = 20,
        max_output_bytes: int = 64 * 1024,
        timeout_seconds: int = 30,
        execution_allowed: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_paths", max_paths),
            ("max_context_lines", max_context_lines),
            ("max_output_bytes", max_output_bytes),
            ("timeout_seconds", timeout_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_paths = max_paths
        self.max_context_lines = max_context_lines
        self.max_output_bytes = max_output_bytes
        self.timeout_seconds = timeout_seconds
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        # Trusted host setting, never a model argument. The caller must establish
        # isolation before allowing repository-configured subprocesses.
        self.execution_allowed = execution_allowed
        self.git_env = self._git_environment()
        self.git_env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self.workspace_root),
            }
        )
        self.runner = ProcessRunner(max_output_bytes=max_output_bytes, base_env=self.git_env)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_diff",
            description=(
                "Show Git changes for a repository inside the workspace. "
                "mode='working' shows unstaged tracked changes, "
                "mode='staged' shows staged changes, and "
                "mode='head' shows tracked changes relative to HEAD. "
                "Untracked files and submodule worktree dirtiness are not included; "
                "submodule commit changes are shown in short format. "
                "External diff programs and text conversion are disabled. "
                "Repositories with external clean/process filters require isolated execution. "
                "If output is truncated, retry with paths to narrow the diff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": ("Workspace-relative directory inside the Git repository."),
                    },
                    "mode": {
                        "type": "string",
                        "enum": [
                            "working",
                            "staged",
                            "head",
                        ],
                        "default": "working",
                    },
                    "paths": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "maxItems": self.max_paths,
                        "description": (
                            "Optional workspace-relative literal paths "
                            "to restrict the diff. A final symbolic link selects "
                            "the link itself; its resolved target is still checked "
                            "against workspace, repository, and credential restrictions."
                        ),
                    },
                    "context_lines": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": self.max_context_lines,
                        "default": 3,
                    },
                },
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments.keys()) - {"cwd", "mode", "paths", "context_lines"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: cwd, mode, paths, context_lines.",
            )
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "cwd must be a non-empty string without NUL characters.",
            )
        mode = arguments.get("mode", "working")
        if not isinstance(mode, str) or mode not in {"working", "staged", "head"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "mode must be 'working', 'staged', or 'head'."
            )
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > self.max_paths:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"paths must be an array with at most {self.max_paths} items.",
            )
        for path in paths:
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    "Every path must be a non-empty string without NUL characters.",
                )
        context_lines = arguments.get("context_lines", 3)
        if (
            type(context_lines) is not int
            or context_lines < 0
            or context_lines > self.max_context_lines
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"context_lines must be an integer between 0 and {self.max_context_lines}.",
            )

        try:
            target_cwd = (self.workspace_root / cwd).resolve()
            if is_credential_path(self.workspace_root / cwd, target_cwd):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target_cwd.is_relative_to(self.workspace_root):
                return tool_error(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "cwd must stay inside the workspace."
                )
            if not target_cwd.is_dir():
                return tool_error(
                    ToolErrorCode.NOT_A_DIRECTORY, "cwd must refer to an existing directory."
                )
        except (OSError, RuntimeError):
            return tool_error("INVALID_WORKING_DIRECTORY", "Unable to resolve cwd.")

        # Discover repository root.
        try:
            repo_result = self.runner.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "rev-parse",
                    "--show-toplevel",
                ],
                cwd=target_cwd,
                timeout_seconds=self.timeout_seconds,
            )
        except ProcessStartError as error:
            if isinstance(error.cause, FileNotFoundError):
                return tool_error("GIT_NOT_FOUND", "Git is not installed or cannot be found.")
            if isinstance(error.cause, PermissionError):
                return tool_error(ToolErrorCode.PERMISSION_DENIED, "Git cannot be executed.")
            return tool_error("GIT_ERROR", "Unable to start Git repository detection.")

        if repo_result.timed_out:
            return tool_error("GIT_TIMEOUT", "Git repository detection timed out.")
        if repo_result.exit_code != 0:
            return tool_error("NOT_A_GIT_REPOSITORY", self._git_error_message(repo_result.stderr))
        repo_text = repo_result.stdout.strip()
        if not repo_text:
            return tool_error("GIT_ERROR", "Git did not return a repository root.")

        try:
            repo_root = Path(repo_text).resolve()
        except (OSError, RuntimeError):
            return tool_error("GIT_ERROR", "Unable to resolve the Git repository root.")
        if not repo_root.is_relative_to(self.workspace_root):
            return tool_error(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                "The Git repository root must stay inside the workspace.",
            )
        repo_relative = repo_root.relative_to(self.workspace_root).as_posix()
        if is_credential_path(self.workspace_root / repo_relative, repo_root):
            return tool_error(ToolErrorCode.PROTECTED_FILE)

        # Validate and convert paths to repo-relative literal pathspecs.
        git_paths: list[str] = []
        for path in paths:
            try:
                requested = self.workspace_root / path
                candidate = requested.resolve()
                # Git tracks the final symlink entry, not its destination. Parent
                # aliases can still resolve to the directory containing that entry.
                git_entry = (
                    requested.parent.resolve() / requested.name
                    if requested.is_symlink()
                    else candidate
                )
                if is_credential_path(requested, candidate) or is_credential_path(
                    git_entry, candidate
                ):
                    return tool_error(
                        ToolErrorCode.PROTECTED_FILE, f"Path {path!r} cannot be accessed by tools."
                    )
                if not all(
                    entry.is_relative_to(self.workspace_root) for entry in (candidate, git_entry)
                ):
                    return tool_error(
                        ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                        f"Path {path!r} must stay inside the workspace.",
                    )
                if not all(entry.is_relative_to(repo_root) for entry in (candidate, git_entry)):
                    return tool_error(
                        "PATH_OUTSIDE_REPOSITORY",
                        f"Path {path!r} is outside the selected repository.",
                    )
                git_paths.append(git_entry.relative_to(repo_root).as_posix())
            except (OSError, RuntimeError):
                return tool_error("INVALID_PATH", f"Unable to resolve path {path!r}.")

        command = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "--no-pager",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--submodule=short",
            "--ignore-submodules=dirty",
            "--no-color",
            f"--unified={context_lines}",
        ]
        if mode == "staged":
            command.append("--cached")
        elif mode == "head":
            command.append("HEAD")
        if git_paths:
            command.append("--")
            command.extend(git_paths)
        try:
            failure = self._check_external_filters(repo_root)
            if failure is not None:
                return failure
            # Enumerate without patch content first, including deleted paths.
            # Disable renames so an allowed destination cannot reveal a protected
            # source's historical content through rename/copy detection.
            name_command = [arg for arg in command if not arg.startswith("--unified=")]
            index = name_command.index("diff") + 1
            name_command[index:index] = ["--no-renames", "--name-only", "-z"]
            result = self.runner.run(
                name_command, cwd=repo_root, timeout_seconds=self.timeout_seconds
            )
            if not result.timed_out and result.exit_code == 0:
                if result.stdout_truncated:
                    return tool_error("OUTPUT_TOO_LARGE", "Too many changed paths; narrow paths.")
                safe_paths = []
                for name in result.stdout.split("\0"):
                    if not name:
                        continue
                    requested = repo_root / name
                    try:
                        resolved = requested.resolve()
                        if not resolved.is_relative_to(repo_root) or is_credential_path(
                            requested, resolved
                        ):
                            continue
                    except (OSError, RuntimeError):
                        return tool_error("GIT_ERROR", "Unable to check changed paths.")
                    safe_paths.append(name)
                if safe_paths:
                    # Re-read config before the second Git invocation as well.
                    failure = self._check_external_filters(repo_root)
                    if failure is not None:
                        return failure
                    # Replace directory selectors with the verified literal files.
                    prefix = command[: command.index("--")] if "--" in command else command
                    result = self.runner.run(
                        [*prefix, "--no-renames", "--", *safe_paths],
                        cwd=repo_root,
                        timeout_seconds=self.timeout_seconds,
                    )
                else:
                    result = replace(result, stdout="")
        except ProcessStartError as error:
            if isinstance(error.cause, FileNotFoundError):
                return tool_error("GIT_NOT_FOUND", "Git is not installed or cannot be found.")
            if isinstance(error.cause, PermissionError):
                return tool_error(ToolErrorCode.PERMISSION_DENIED, "Git cannot be executed.")
            return tool_error("GIT_ERROR", "Unable to start Git.")

        if result.timed_out:
            return tool_error("GIT_TIMEOUT", "git diff timed out.")
        if result.exit_code != 0:
            return tool_error("GIT_ERROR", self._git_error_message(result.stderr))
        return ToolResult(
            success=True,
            data={
                "repo_root": repo_relative,
                "mode": mode,
                "paths": paths,
                "context_lines": context_lines,
                "has_changes": bool(result.stdout),
                "diff": result.stdout,
                "truncated": result.stdout_truncated,
            },
        )

    @staticmethod
    def _git_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LITERAL_PATHSPECS"] = "1"
        return env

    @staticmethod
    def _git_error_message(stderr: str) -> str:
        message = stderr.strip()
        if not message:
            return "Git command failed."
        # Avoid returning arbitrarily large infrastructure errors.
        return message[:2000]


# GitStatusTool
_STATUS_MAP = {
    ".": None,
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "U": "unmerged",
}


class GitStatusTool(_GitFilterPolicy):
    """Return structured Git working-tree status."""

    execution_kind = ExecutionKind.SANDBOXED_PROCESS
    scheduling_policy = SERIAL

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_paths: int = 64,
        max_entries: int = 500,
        max_output_bytes: int = 64 * 1024,
        timeout_seconds: int = 30,
        execution_allowed: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_paths", max_paths),
            ("max_entries", max_entries),
            ("max_output_bytes", max_output_bytes),
            ("timeout_seconds", timeout_seconds),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_paths = max_paths
        self.max_entries = max_entries
        self.max_output_bytes = max_output_bytes
        self.timeout_seconds = timeout_seconds
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        self.execution_allowed = execution_allowed
        self.git_env = self._git_environment()
        self.git_env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(self.workspace_root),
            }
        )
        self.runner = ProcessRunner(max_output_bytes=max_output_bytes, base_env=self.git_env)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="git_status",
            description=(
                "Show structured Git working-tree status for a repository "
                "inside the workspace. Reports staged, unstaged, renamed, "
                "deleted, conflicted, and untracked paths. "
                "Submodule worktree dirtiness is ignored; commit changes are still reported. "
                "External clean/process filters require isolated execution. "
                "All returned paths are workspace-relative; untracked files are listed "
                "individually. Credential paths, including either side of a rename, are "
                "excluded. scope_clean only describes visible changes in the returned "
                "scope (paths and include_untracked), not overall repository cleanliness. "
                "Counts exclude protected paths; total_entries and scope_clean are computed "
                "before the output entry limit. "
                "Use git_diff to inspect actual content changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": "Workspace-relative directory selecting the repository; "
                        "does not restrict status to this directory.",
                    },
                    "paths": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "maxItems": self.max_paths,
                        "description": "Literal workspace-relative files or directories to query. "
                        "An empty array selects the entire repository.",
                    },
                    "include_untracked": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include individual untracked files in the query scope.",
                    },
                },
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments.keys()) - {"cwd", "paths", "include_untracked"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: cwd, paths, include_untracked.",
            )
        cwd = arguments.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "cwd must be a non-empty string without NUL characters.",
            )
        paths = arguments.get("paths", [])
        if not isinstance(paths, list) or len(paths) > self.max_paths:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"paths must be an array with at most {self.max_paths} items.",
            )
        for path in paths:
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    "Every path must be a non-empty string without NUL characters.",
                )
        include_untracked = arguments.get("include_untracked", True)
        if not isinstance(include_untracked, bool):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "include_untracked must be a boolean.",
            )

        try:
            target_cwd = (self.workspace_root / cwd).resolve()
            if is_credential_path(self.workspace_root / cwd, target_cwd):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target_cwd.is_relative_to(self.workspace_root):
                return tool_error(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "cwd must stay inside the workspace."
                )
            if not target_cwd.is_dir():
                return tool_error(
                    ToolErrorCode.NOT_A_DIRECTORY, "cwd must refer to an existing directory."
                )
        except (OSError, RuntimeError):
            return tool_error("INVALID_WORKING_DIRECTORY", "Unable to resolve cwd.")

        # Discover repository root.
        try:
            repo_result = self.runner.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "core.fsmonitor=false",
                    "rev-parse",
                    "--show-toplevel",
                ],
                cwd=target_cwd,
                timeout_seconds=self.timeout_seconds,
            )
        except ProcessStartError as error:
            if isinstance(error.cause, FileNotFoundError):
                return tool_error("GIT_NOT_FOUND", "Git is not installed or cannot be found.")
            if isinstance(error.cause, PermissionError):
                return tool_error(ToolErrorCode.PERMISSION_DENIED, "Git cannot be executed.")
            return tool_error("GIT_ERROR", "Unable to start Git repository detection.")

        if repo_result.timed_out:
            return tool_error("GIT_TIMEOUT", "Git repository detection timed out.")
        if repo_result.exit_code != 0:
            return tool_error("NOT_A_GIT_REPOSITORY", self._git_error_message(repo_result.stderr))
        if repo_result.stdout_truncated:
            return tool_error(
                "GIT_STATUS_OUTPUT_TOO_LARGE", "Git repository root output was truncated."
            )
        repo_text = repo_result.stdout.removesuffix("\n")
        if not repo_text:
            return tool_error("GIT_ERROR", "Git did not return a repository root.")

        try:
            repo_root = Path(repo_text).resolve()
        except (OSError, RuntimeError):
            return tool_error("GIT_ERROR", "Unable to resolve the Git repository root.")
        if not repo_root.is_relative_to(self.workspace_root):
            return tool_error(
                ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                "The Git repository root must stay inside the workspace.",
            )
        repo_relative = repo_root.relative_to(self.workspace_root).as_posix()
        if is_credential_path(self.workspace_root / repo_relative, repo_root):
            return tool_error(ToolErrorCode.PROTECTED_FILE)

        # Validate and convert paths to repo-relative literal pathspecs.
        git_paths: list[str] = []
        for path in paths:
            try:
                requested = self.workspace_root / path
                candidate = requested.resolve()
                # Git tracks the final symlink entry, not its destination. Parent
                # aliases can still resolve to the directory containing that entry.
                git_entry = (
                    requested.parent.resolve() / requested.name
                    if requested.is_symlink()
                    else candidate
                )
                if is_credential_path(requested, candidate) or is_credential_path(
                    git_entry, candidate
                ):
                    return tool_error(
                        ToolErrorCode.PROTECTED_FILE, f"Path {path!r} cannot be accessed by tools."
                    )
                if not all(
                    entry.is_relative_to(self.workspace_root) for entry in (candidate, git_entry)
                ):
                    return tool_error(
                        ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                        f"Path {path!r} must stay inside the workspace.",
                    )
                if not all(entry.is_relative_to(repo_root) for entry in (candidate, git_entry)):
                    return tool_error(
                        "PATH_OUTSIDE_REPOSITORY",
                        f"Path {path!r} is outside the selected repository.",
                    )
                git_paths.append(git_entry.relative_to(repo_root).as_posix())
            except (OSError, RuntimeError):
                return tool_error("INVALID_PATH", f"Unable to resolve path {path!r}.")

        command = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "--no-pager",
            "--no-optional-locks",
            "status",
            "--porcelain=v2",
            "-z",
            "--branch",
            "--ignore-submodules=dirty",
            ("--untracked-files=all" if include_untracked else "--untracked-files=no"),
        ]
        if git_paths:
            command.append("--")
            command.extend(git_paths)
        try:
            failure = self._check_external_filters(repo_root)
            if failure is not None:
                return failure
            result = self.runner.run(
                command,
                cwd=repo_root,
                timeout_seconds=self.timeout_seconds,
            )
        except ProcessStartError as error:
            if isinstance(error.cause, FileNotFoundError):
                return tool_error("GIT_NOT_FOUND", "Git is not installed or cannot be found.")
            if isinstance(error.cause, PermissionError):
                return tool_error(ToolErrorCode.PERMISSION_DENIED, "Git cannot be executed.")
            return tool_error("GIT_ERROR", "Unable to start Git.")

        if result.timed_out:
            return tool_error("GIT_TIMEOUT", "git status timed out.")
        if result.exit_code != 0:
            return tool_error("GIT_ERROR", self._git_error_message(result.stderr))
        # A truncated NUL stream is unsafe to parse.
        if result.stdout_truncated:
            return tool_error(
                "GIT_STATUS_OUTPUT_TOO_LARGE",
                "Git status output exceeded the configured output limit. "
                "Restrict the request with paths or disable untracked files.",
            )
        try:
            parsed = self._parse_porcelain_v2(result.stdout)
        except ValueError:
            return tool_error("GIT_PARSE_ERROR", "Unable to parse Git porcelain status output.")
        try:
            entries = self._visible_entries(parsed["entries"], repo_root)
        except ValueError:
            return tool_error("GIT_PARSE_ERROR", "Git returned an invalid status path.")
        except (OSError, RuntimeError):
            return tool_error("INVALID_PATH", "Unable to check Git status paths.")
        truncated = len(entries) > self.max_entries
        returned_entries = entries[: self.max_entries]
        repo_relative = repo_root.relative_to(self.workspace_root).as_posix()
        return ToolResult(
            success=True,
            data={
                "repo_root": repo_relative,
                "branch": parsed["branch"],
                "head": parsed["head"],
                "detached": parsed["detached"],
                "upstream": parsed["upstream"],
                "ahead": parsed["ahead"],
                "behind": parsed["behind"],
                "scope": {
                    "paths": [
                        (repo_root / path).relative_to(self.workspace_root).as_posix()
                        for path in git_paths
                    ],
                    "include_untracked": include_untracked,
                    "protected_paths_excluded": True,
                },
                "scope_clean": len(entries) == 0,
                "entries": returned_entries,
                "total_entries": len(entries),
                "returned_entries": len(returned_entries),
                "truncated": truncated,
            },
        )

    def _visible_entries(
        self, entries: list[dict[str, Any]], repo_root: Path
    ) -> list[dict[str, Any]]:
        visible = []
        for entry in entries:
            converted = dict(entry)
            protected = False
            for key in ("path", "original_path"):
                value = entry[key]
                if value is None:
                    continue
                relative = Path(value)
                if not value or relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Invalid repository-relative path")
                requested = repo_root / relative
                if is_credential_path(requested, requested.resolve()):
                    protected = True
                    break
                # Resolve only for protection checks: Git reports the symlink entry itself.
                converted[key] = requested.relative_to(self.workspace_root).as_posix()
            if not protected:
                visible.append(converted)
        return visible

    @classmethod
    def _parse_porcelain_v2(
        cls,
        output: str,
    ) -> dict[str, Any]:
        records = output.split("\x00")
        branch: str | None = None
        head: str | None = None
        upstream: str | None = None
        ahead = 0
        behind = 0
        detached = False
        entries: list[dict[str, Any]] = []

        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if not record:
                continue
            if record.startswith("# "):
                if record.startswith("# branch.oid "):
                    value = record[len("# branch.oid ") :]
                    if value != "(initial)":
                        head = value
                elif record.startswith("# branch.head "):
                    value = record[len("# branch.head ") :]
                    if value == "(detached)":
                        detached = True
                        branch = None
                    else:
                        branch = value
                elif record.startswith("# branch.upstream "):
                    upstream = record[len("# branch.upstream ") :]
                elif record.startswith("# branch.ab "):
                    value = record[len("# branch.ab ") :]
                    parts = value.split()
                    for part in parts:
                        if part.startswith("+"):
                            ahead = int(part[1:])
                        elif part.startswith("-"):
                            behind = int(part[1:])
                continue

            record_type = record[0]
            if record_type == "?":
                entries.append(
                    {
                        "path": record[2:],
                        "original_path": None,
                        "staged_status": None,
                        "worktree_status": None,
                        "conflicted": False,
                        "untracked": True,
                    }
                )
                continue
            if record_type == "!":
                # Normally impossible unless --ignored is added.
                continue
            if record_type == "1":
                fields = record.split(" ", 8)
                if len(fields) != 9:
                    raise ValueError
                _, xy, sub, *_metadata, path = fields
                entries.append(
                    cls._make_changed_entry(
                        path=path,
                        original_path=None,
                        xy=xy,
                        sub=sub,
                        conflicted=False,
                    )
                )
                continue
            if record_type == "2":
                fields = record.split(" ", 9)
                if len(fields) != 10:
                    raise ValueError
                _, xy, sub, *_metadata, score, path = fields
                if i >= len(records):
                    raise ValueError
                original_path = records[i]
                i += 1
                entry = cls._make_changed_entry(
                    path=path,
                    original_path=original_path,
                    xy=xy,
                    sub=sub,
                    conflicted=False,
                )
                entry["similarity"] = score
                entries.append(entry)
                continue
            if record_type == "u":
                fields = record.split(" ", 10)
                if len(fields) != 11:
                    raise ValueError
                _, xy, sub, *_metadata, path = fields
                entries.append(
                    cls._make_changed_entry(
                        path=path,
                        original_path=None,
                        xy=xy,
                        sub=sub,
                        conflicted=True,
                    )
                )
                continue
            raise ValueError

        return {
            "branch": branch,
            "head": head,
            "detached": detached,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "entries": entries,
        }

    @classmethod
    def _make_changed_entry(
        cls,
        *,
        path: str,
        original_path: str | None,
        xy: str,
        sub: str,
        conflicted: bool,
    ) -> dict[str, Any]:
        if len(xy) != 2:
            raise ValueError
        staged = _STATUS_MAP.get(xy[0], "unknown")
        worktree = _STATUS_MAP.get(xy[1], "unknown")
        entry: dict[str, Any] = {
            "path": path,
            "original_path": original_path,
            "staged_status": staged,
            "worktree_status": worktree,
            "conflicted": conflicted,
            "untracked": False,
        }
        if sub.startswith("S") and len(sub) == 4:
            entry["submodule"] = {
                "commit_changed": sub[1] == "C",
                "modified": sub[2] == "M",
                "untracked": sub[3] == "U",
            }
        return entry

    @staticmethod
    def _git_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LITERAL_PATHSPECS"] = "1"
        return env

    @staticmethod
    def _git_error_message(stderr: str) -> str:
        message = stderr.strip()
        if not message:
            return "Git command failed."
        # Avoid returning arbitrarily large infrastructure errors.
        return message[:2000]
