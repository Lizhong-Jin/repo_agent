"""Build the default tool set for a workspace."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from host_support.read_budget import ReadLimits

from ._internal.base import Tool, validate_tools
from ._internal.lsp_config import LspRegistry, default_lsp_registry
from .execute import (
    GetExecutionEnvironmentTool,
    RunCommandTool,
    RunPythonTool,
    RunShellTool,
)
from .filesystem import (
    ApplyPatchTool,
    DeleteFileTool,
    EditFileTool,
    FindFileTool,
    GetPathInfoTool,
    ListFileTool,
    MakeDirectoryTool,
    MoveFileTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from .git_tools import (
    GitDiffTool,
    GitLogTool,
    GitShowTool,
    GitStatusTool,
)
from .semantic import (
    FindReferencesTool,
    GetDiagnosticsTool,
    GetHoverTool,
    GetSymbolsTool,
    GoToDefinitionsTool,
    SearchWorkspaceSymbolsTool,
)


def create_file_tools(
    workspace_root: str | Path, *, read_limits: ReadLimits | None = None
) -> list[Tool]:
    """Only audited, built-in file implementations may run in the lightweight layer."""
    return validate_tools(
        [
            ReadFileTool(workspace_root, read_limits=read_limits),
            WriteFileTool(workspace_root),
            EditFileTool(workspace_root),
            ApplyPatchTool(workspace_root),
            ListFileTool(workspace_root, read_limits=read_limits),
            FindFileTool(workspace_root, read_limits=read_limits),
            SearchFilesTool(workspace_root, read_limits=read_limits),
            MakeDirectoryTool(workspace_root),
            DeleteFileTool(workspace_root),
            MoveFileTool(workspace_root),
            GetPathInfoTool(workspace_root, read_limits=read_limits),
        ]
    )


def create_default_tools(
    workspace_root: str | Path,
    *,
    isolated_execution: bool = False,
    lsp_registry: LspRegistry | None = None,
    command_timeout_seconds: int = 120,
    python_timeout_seconds: int = 30,
    execution_context: Mapping[str, Any] | None = None,
    read_limits: ReadLimits | None = None,
) -> list[Tool]:
    """Commands are exposed only by callers providing an isolated environment.

    isolated_execution is a trusted application switch, never a model argument.
    It does not establish isolation itself; Docker and native workers enable it.
    """
    if type(isolated_execution) is not bool:
        raise ValueError("isolated_execution must be a boolean")
    return validate_tools(
        [
            GetExecutionEnvironmentTool(
                workspace_root,
                execution_allowed=isolated_execution,
                execution_context=execution_context,
                command_timeout_seconds=command_timeout_seconds,
                python_timeout_seconds=python_timeout_seconds,
            ),
            *create_file_tools(workspace_root, read_limits=read_limits),
            # command and code_intelligence tools
            *(
                [
                    RunCommandTool(
                        workspace_root,
                        execution_allowed=True,
                        default_timeout_seconds=min(60, command_timeout_seconds),
                        max_timeout_seconds=command_timeout_seconds,
                    ),
                    RunShellTool(
                        workspace_root,
                        execution_allowed=True,
                        default_timeout_seconds=min(60, command_timeout_seconds),
                        max_timeout_seconds=command_timeout_seconds,
                    ),
                    RunPythonTool(
                        workspace_root,
                        execution_allowed=True,
                        default_timeout_seconds=min(10, python_timeout_seconds),
                        max_timeout_seconds=python_timeout_seconds,
                    ),
                    GetSymbolsTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                    GoToDefinitionsTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                    FindReferencesTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                    GetDiagnosticsTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                    GetHoverTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                    SearchWorkspaceSymbolsTool(
                        workspace_root,
                        execution_allowed=True,
                        lsp_registry=lsp_registry
                        if lsp_registry is not None
                        else default_lsp_registry(),
                    ),
                ]
                if isolated_execution
                else []
            ),
            # git tools
            GitDiffTool(workspace_root, execution_allowed=isolated_execution),
            GitLogTool(workspace_root, execution_allowed=isolated_execution),
            GitShowTool(workspace_root, execution_allowed=isolated_execution),
            GitStatusTool(workspace_root, execution_allowed=isolated_execution),
        ]
    )
