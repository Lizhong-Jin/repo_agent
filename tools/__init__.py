"""Tools available to the coding agent."""

from ._internal.base import ExecutionKind, Tool, ToolResult
from ._internal.errors import ToolErrorCode, tool_error
from ._internal.process_runner import ProcessResult, ProcessRunner, ProcessStartError
from .dispatch import ToolDispatcher
from .execute import (
    GetExecutionEnvironmentTool,
    RunCommandTool,
    RunPythonTool,
    RunShellTool,
)
from .factory import create_default_tools
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
from .tool_groups import DEFAULT_TOOL_GROUPS, LoadToolGroupTool, ToolGroup

__all__ = [
    # semantic tools
    "GetSymbolsTool",
    "GoToDefinitionsTool",
    "FindReferencesTool",
    "GetDiagnosticsTool",
    "GetHoverTool",
    "SearchWorkspaceSymbolsTool",
    # filesystem tools
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "ApplyPatchTool",
    "ListFileTool",
    "FindFileTool",
    "SearchFilesTool",
    "MakeDirectoryTool",
    "DeleteFileTool",
    "MoveFileTool",
    "GetPathInfoTool",
    # command tools
    "GetExecutionEnvironmentTool",
    "RunCommandTool",
    "RunPythonTool",
    "RunShellTool",
    # git tools
    "GitDiffTool",
    "GitStatusTool",
    # process runner
    "ProcessRunner",
    "ProcessResult",
    "ProcessStartError",
    # other
    "Tool",
    "ExecutionKind",
    "ToolDispatcher",
    "ToolResult",
    "ToolErrorCode",
    "tool_error",
    "create_default_tools",
    "DEFAULT_TOOL_GROUPS",
    "LoadToolGroupTool",
    "ToolGroup",
]
