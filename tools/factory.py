"""Build the default tool set for a workspace."""

from pathlib import Path

from .base import Tool
from .execute import (
    RunCommandTool,
    RunPythonTool,
)
from .filesystem import (
    BatchEditFileTool,
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
    GitStatusTool,
)
from .lsp_config import LspRegistry, default_lsp_registry
from .semantic import GetSymbolsTool


def create_default_tools(
    workspace_root: str | Path,
    *,
    isolated_execution: bool = False,
    lsp_registry: LspRegistry | None = None,
    command_timeout_seconds: int = 120,
    python_timeout_seconds: int = 30,
) -> list[Tool]:
    """Commands are exposed only by callers providing an isolated environment.

    isolated_execution is a trusted application switch, never a model argument.
    It does not establish isolation itself; only the Docker worker enables it.
    """
    if type(isolated_execution) is not bool:
        raise ValueError("isolated_execution must be a boolean")
    return [
        # filesystem tools
        ReadFileTool(workspace_root),
        WriteFileTool(workspace_root),
        EditFileTool(workspace_root),
        BatchEditFileTool(workspace_root),
        ListFileTool(workspace_root),
        FindFileTool(workspace_root),
        SearchFilesTool(workspace_root),
        MakeDirectoryTool(workspace_root),
        DeleteFileTool(workspace_root),
        MoveFileTool(workspace_root),
        GetPathInfoTool(workspace_root),
        # command and code_intelligence tools
        *(
            [
                RunCommandTool(
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
            ]
            if isolated_execution
            else []
        ),
        # git tools
        GitDiffTool(workspace_root),
        GitStatusTool(workspace_root),
    ]
