"""Build the default tool set for a workspace."""

from collections.abc import Mapping
from functools import partial
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
    workspace_root: str | Path,
    *,
    read_limits: ReadLimits | None = None,
    read_only=False,
    tool_name: str | None = None,
) -> list[Tool]:
    """Build only requested, audited file tools; selection grants no privileges."""
    builders = {
        "read_file": partial(ReadFileTool, read_limits=read_limits),
        "write_file": WriteFileTool,
        "edit_file": EditFileTool,
        "apply_patch": ApplyPatchTool,
        "list_files": partial(ListFileTool, read_limits=read_limits),
        "find_files": partial(FindFileTool, read_limits=read_limits),
        "search_files": partial(SearchFilesTool, read_limits=read_limits),
        "make_directory": MakeDirectoryTool,
        "delete_file": DeleteFileTool,
        "move_file": MoveFileTool,
        "get_path_info": partial(GetPathInfoTool, read_limits=read_limits),
    }
    tools = validate_tools(
        [
            build(workspace_root)
            for name, build in builders.items()
            if tool_name is None or tool_name == name
        ]
    )
    if read_only:
        from .access_policy import AccessPolicy

        tools = [tool for tool in tools if AccessPolicy("review").allows(tool)]
        for tool in tools:
            tool.read_only = True
    return tools


def create_default_tools(
    workspace_root: str | Path,
    *,
    isolated_execution: bool = False,
    lsp_registry: LspRegistry | None = None,
    command_timeout_seconds: int = 120,
    python_timeout_seconds: int = 30,
    execution_context: Mapping[str, Any] | None = None,
    workspace_kind: str = "direct",
    read_limits: ReadLimits | None = None,
    read_only: bool = False,
    tool_name: str | None = None,
) -> list[Tool]:
    """Build the catalog, or just one tool without constructing unrelated tools.

    isolated_execution is a trusted application switch, never a model argument.
    Tool selection does not change execution policy; unknown names return no tools.
    """
    if type(isolated_execution) is not bool:
        raise ValueError("isolated_execution must be a boolean")
    if read_only and isolated_execution:
        raise ValueError("Review mode does not allow process execution")
    builders = {
        "get_execution_environment": partial(
            GetExecutionEnvironmentTool,
            execution_allowed=isolated_execution,
            execution_context=execution_context,
            workspace_kind=workspace_kind,
            read_only=read_only,
            command_timeout_seconds=command_timeout_seconds,
            python_timeout_seconds=python_timeout_seconds,
        ),
    }
    if isolated_execution:
        builders.update(
            {
                "run_command": partial(
                    RunCommandTool,
                    execution_allowed=True,
                    default_timeout_seconds=min(60, command_timeout_seconds),
                    max_timeout_seconds=command_timeout_seconds,
                ),
                "run_shell": partial(
                    RunShellTool,
                    execution_allowed=True,
                    default_timeout_seconds=min(60, command_timeout_seconds),
                    max_timeout_seconds=command_timeout_seconds,
                ),
                "run_python": partial(
                    RunPythonTool,
                    execution_allowed=True,
                    default_timeout_seconds=min(10, python_timeout_seconds),
                    max_timeout_seconds=python_timeout_seconds,
                ),
            }
        )
        for name, tool_type in {
            "get_symbols": GetSymbolsTool,
            "go_to_definition": GoToDefinitionsTool,
            "find_references": FindReferencesTool,
            "get_diagnostics": GetDiagnosticsTool,
            "get_hover": GetHoverTool,
            "search_workspace_symbols": SearchWorkspaceSymbolsTool,
        }.items():
            builders[name] = partial(
                tool_type,
                execution_allowed=True,
                lsp_registry=lsp_registry if lsp_registry is not None else default_lsp_registry(),
            )
    for name, tool_type in {
        "git_diff": GitDiffTool,
        "git_log": GitLogTool,
        "git_show": GitShowTool,
        "git_status": GitStatusTool,
    }.items():
        builders[name] = partial(tool_type, execution_allowed=isolated_execution)
    tools = validate_tools(
        [
            build(workspace_root)
            for name, build in builders.items()
            if tool_name is None or tool_name == name
        ]
    )
    files = create_file_tools(
        workspace_root,
        read_limits=read_limits,
        read_only=read_only,
        tool_name=tool_name,
    )
    # Preserve the public catalog order: environment, files, execution, Git.
    offset = 1 if tools and tools[0].definition.name == "get_execution_environment" else 0
    tools[offset:offset] = files
    if read_only:
        from ._internal.review_git import ReviewGitRunner

        for tool in tools:
            if type(tool) in {GitDiffTool, GitLogTool, GitShowTool, GitStatusTool}:
                tool.runner = ReviewGitRunner(tool.runner, workspace_root)
    return tools
