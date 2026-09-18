"""Shared tool error codes and result construction; tool-specific codes remain extensible."""

from enum import StrEnum

from .base import ToolResult


class ToolErrorCode(StrEnum):
    """Stable wire values: callers and existing logs can keep using the same strings."""

    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    PROTECTED_FILE = "PROTECTED_FILE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PARENT_NOT_FOUND = "PARENT_NOT_FOUND"
    NOT_A_FILE = "NOT_A_FILE"
    NOT_A_DIRECTORY = "NOT_A_DIRECTORY"
    PARENT_NOT_DIRECTORY = "PARENT_NOT_DIRECTORY"
    PATH_IS_SYMLINK = "PATH_IS_SYMLINK"
    FILE_EXISTS = "FILE_EXISTS"
    FILE_CHANGED = "FILE_CHANGED"
    PATH_ALREADY_EXISTS = "PATH_ALREADY_EXISTS"
    BINARY_FILE = "BINARY_FILE"
    UNSUPPORTED_ENCODING = "UNSUPPORTED_ENCODING"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    READ_ERROR = "READ_ERROR"
    WRITE_ERROR = "WRITE_ERROR"


_DEFAULT_MESSAGES = {
    ToolErrorCode.INVALID_ARGUMENTS: "Arguments must be an object.",
    ToolErrorCode.PATH_OUTSIDE_WORKSPACE: "The path must stay inside the workspace.",
    ToolErrorCode.PROTECTED_FILE: "Protected paths cannot be accessed by tools.",
    ToolErrorCode.PERMISSION_DENIED: "The operation is not allowed with current permissions.",
    ToolErrorCode.FILE_NOT_FOUND: "The requested file or directory does not exist.",
    ToolErrorCode.PARENT_NOT_FOUND: "A required parent directory does not exist.",
    ToolErrorCode.NOT_A_FILE: "The path must refer to a regular file.",
    ToolErrorCode.NOT_A_DIRECTORY: "The path must refer to a directory.",
    ToolErrorCode.PARENT_NOT_DIRECTORY: "The parent path must be a directory.",
    ToolErrorCode.PATH_IS_SYMLINK: "The path must not be a symlink.",
    ToolErrorCode.FILE_EXISTS: "The file already exists and overwrite is false.",
    ToolErrorCode.FILE_CHANGED: "The file has been modified. Please re-read it.",
    ToolErrorCode.PATH_ALREADY_EXISTS: "The path exists but has an incompatible type.",
    ToolErrorCode.BINARY_FILE: "Binary files are not supported.",
    ToolErrorCode.UNSUPPORTED_ENCODING: "Text must use valid UTF-8 encoding.",
    ToolErrorCode.FILE_TOO_LARGE: "The file exceeds the allowed size.",
    ToolErrorCode.CONTENT_TOO_LARGE: "The content exceeds the allowed size.",
    ToolErrorCode.OUTPUT_TOO_LARGE: "The result exceeds the allowed output size.",
    ToolErrorCode.READ_ERROR: "Unable to resolve or read the path.",
    ToolErrorCode.WRITE_ERROR: "Unable to write the file.",
}


def tool_error(code: ToolErrorCode | str, message: str | None = None) -> ToolResult:
    """Use a shared default or a specific explanation; custom codes require a message."""
    if message is None:
        try:
            message = _DEFAULT_MESSAGES[code]
        except KeyError:
            raise ValueError("Tool-specific error codes require an explicit message") from None
    return ToolResult(success=False, error_code=str(code), error=message)
