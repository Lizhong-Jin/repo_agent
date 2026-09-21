"""Tools available to the coding agent."""

from .base import Tool, ToolResult
from .errors import ToolErrorCode, tool_error
from .execute import (
    GetExecutionEnvironmentTool,
    RunCommandTool,
    RunPythonTool,
)
from .factory import create_default_tools
from .filesystem import (
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
from .process_runner import ProcessResult, ProcessRunner, ProcessStartError
from .semantic import (
    FindReferencesTool,
    GetDiagnosticsTool,
    GetSymbolsTool,
    GoToDefinitionsTool,
)

__all__ = [
    # code_intelligence tools
    "GetSymbolsTool",
    "GoToDefinitionsTool",
    "FindReferencesTool",
    "GetDiagnosticsTool",
    # filesystem tools
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
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
    # git tools
    "GitDiffTool",
    "GitStatusTool",
    # process runner
    "ProcessRunner",
    "ProcessResult",
    "ProcessStartError",
    # other
    "Tool",
    "ToolResult",
    "ToolErrorCode",
    "tool_error",
    "create_default_tools",
]
