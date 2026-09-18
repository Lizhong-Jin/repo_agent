"""
Workspace-scoped file operations. This path guard is not an OS sandbox.

Includes tools related to file operations:
ReadFileTool: Read UTF-8 text files with line ranges and bounded output.
WriteFileTool: Create or replace UTF-8 text files with bounded content.
EditFileTool: Replace one unique text fragment in an existing UTF-8 workspace file.
ListFilesTool: List files in a directory with optional filtering and recursion.
SearchFilesTool: Search for a text fragment in files with optional filtering and recursion.
MakeDirectoryTool: Create a directory and optionally its parents.
DeleteFileTool: Delete a file, ensuring it is not a directory or symlink.

All tools enforce workspace-relative paths and prevent access to credential files.
"""

import logging
import os
import errno
import re
import copy
from pathlib import Path
from stat import S_ISREG, S_ISDIR, S_ISLNK
from dataclasses import asdict, dataclass, field
from typing import Any
import hashlib
from fnmatch import fnmatch
from tempfile import NamedTemporaryFile

from llm import ToolDefinition

from .base import ToolResult
from .errors import ToolErrorCode, tool_error
from .file_policy import is_credential_path

logger = logging.getLogger(__name__)

# ReadFileTool
class ReadFileTool:
    """Read UTF-8 source files with 1-based inclusive line ranges and bounded output."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_lines: int = 200,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_output_chars: int = 20_000,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_lines", max_lines),
            ("max_file_bytes", max_file_bytes),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_lines = max_lines
        self.max_file_bytes = max_file_bytes
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description=(
                "Read a UTF-8 text file inside the workspace. Prefer workspace-relative paths. "
                "Returns content with line numbers. start_line and end_line are 1-based and "
                f"inclusive. Returns at most {self.max_lines} lines per call; "
                "use next_start_line to continue if truncated. "
                "If output is too large, request a smaller line range."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to a file inside the workspace.",
                    },
                    "start_line": {"type": "integer", "minimum": 1, "default": 1},
                    "end_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Inclusive end line; omitted means up to EOF.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "start_line", "end_line"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, start_line, end_line.",
            )
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line")
        if type(start) is not int or start < 1:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "start_line must be a positive integer.",
            )
        if "end_line" in arguments and (type(end) is not int or end < start):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "end_line must be an integer >= start_line.",
            )

        try:
            target = (self.workspace_root / path).resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = target.stat()
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            if info.st_size > self.max_file_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_file_bytes} bytes.",
                )
            with target.open("rb") as source:
                raw = source.read(self.max_file_bytes + 1)
            if len(raw) > self.max_file_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_file_bytes} bytes.",
                )
            if b"\x00" in raw:
                return tool_error(ToolErrorCode.BINARY_FILE)
            # utf-8-sig also accepts plain UTF-8 and strips an optional BOM.
            text = raw.decode("utf-8-sig")
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except UnicodeDecodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR)

        # Recognize LF, CRLF and CR without treating Unicode separators as source-code lines.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.split("\n") if normalized else []
        if normalized.endswith("\n"):
            lines.pop()
        total = len(lines)
        if start > max(total, 1):
            return tool_error("LINE_OUT_OF_RANGE", f"start_line exceeds the file's {total} lines.")
        requested_end = min(end if end is not None else total, total)
        actual_end = min(requested_end, start + self.max_lines - 1)
        content = "\n".join(
            f"{number}: {lines[number - 1]}" for number in range(start, actual_end + 1)
        )
        if len(content) > self.max_output_chars:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE,
                f"Numbered content exceeds {self.max_output_chars} characters. "
                "Request fewer lines; "
                "a single oversized line requires increasing the host's max_output_chars setting.",
            )
        truncated = actual_end < requested_end
        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "content": content,
                "start_line": start if total else None,
                "end_line": actual_end if total else None,
                "total_lines": total,
                "truncated": truncated,
                "next_start_line": actual_end + 1 if truncated else None,
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )


# WriteFileTool
class WriteFileTool:
    """Create or replace bounded UTF-8 text files inside a workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_content_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_content_bytes", max_content_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_content_bytes = max_content_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="write_file",
            description=(
                "Create or completely replace a UTF-8 text file inside the workspace. "
                "Prefer workspace-relative paths. Content is written exactly as provided. "
                "Existing files are not overwritten unless overwrite=true. "
                f"Content must not exceed {self.max_content_bytes} UTF-8 bytes. "
                "Use an edit_file for small changes to existing files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to a file inside the workspace.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete UTF-8 content to write to the file.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to overwrite an existing file. If false and the file exists, the tool returns an error.",
                    },
                    "create_parents": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to create parent directories if they do not exist.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "content", "overwrite", "create_parents"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, content, overwrite, create_parents.",
            )
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        content = arguments.get("content")
        if not isinstance(content, str):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "content must be a string.")
        if "\x00" in content:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "content must not contain NUL bytes.",
            )
        overwrite = arguments.get("overwrite", False)
        create_parents = arguments.get("create_parents", False)
        if type(overwrite) is not bool:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "overwrite must be a boolean.")
        if type(create_parents) is not bool:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "create_parents must be a boolean.")

        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        if len(encoded) > self.max_content_bytes:
            return tool_error(
                ToolErrorCode.CONTENT_TOO_LARGE,
                f"Content exceeds {self.max_content_bytes} UTF-8 bytes.",
            )

        try:
            target = (self.workspace_root / path).resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            parent = target.parent
            if not parent.exists():
                if create_parents:
                    parent.mkdir(parents=True, exist_ok=True)
                else:
                    return tool_error(ToolErrorCode.PARENT_NOT_FOUND)
            if not parent.is_dir():
                return tool_error(ToolErrorCode.PARENT_NOT_DIRECTORY)
            existed = target.exists()
            if existed:
                info = target.stat()
                if not S_ISREG(info.st_mode):
                    return tool_error(ToolErrorCode.NOT_A_FILE)
                if not overwrite:
                    return tool_error(ToolErrorCode.FILE_EXISTS)
                old_mode = target.stat().st_mode

            # Write the file atomically by writing to a temporary file and renaming it.
            temp_path: Path | None = None
            try:
                with NamedTemporaryFile(mode="wb", dir=parent, delete=False) as temp_file:
                    # Save the path before any write/flush/fsync can fail.
                    temp_path = Path(temp_file.name)
                    temp_file.write(encoded)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                if existed:
                    os.chmod(temp_path, old_mode)
                temp_path.replace(target)
            finally:
                if temp_path is not None:
                    try:
                        # Successful replace already removed the temporary path.
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        # Cleanup failure must not replace the original write error.
                        logger.warning(
                            "Unable to remove temporary file %s", temp_path, exc_info=True
                        )

        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.WRITE_ERROR)

        line_count = self._count_lines(content)
        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "bytes_written": len(encoded),
                "lines_written": line_count,
                "created": not existed,
                "overwritten": existed,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
        )

    @staticmethod
    def _count_lines(text: str) -> int:
        if not text:
            return 0
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.count("\n") + (0 if normalized.endswith("\n") else 1)


# EditFileTool
class EditFileTool:
    """Replace one unique text fragment in an existing UTF-8 workspace file."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_content_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_content_bytes", max_content_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_content_bytes = max_content_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_file",
            description=(
                "Edit an existing UTF-8 text file inside the workspace by replacing "
                "one exact text fragment with another. old_text must match exactly once. "
                "Matching treats LF, CRLF and CR as equivalent; overlapping matches "
                "are rejected. The original BOM and text outside the match are preserved. "
                "Include enough surrounding context to make the match unique. "
                "new_text may be empty to delete the matched text. "
                "Use read_file first if the current content is uncertain. "
                "Use write_file to create a new file or replace an entire file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing UTF-8 text file inside the workspace.",
                    },
                    "old_text": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Exact text to replace. It must occur exactly once in the file."
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text. May be empty to delete old_text.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "old_text", "new_text"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, old_text, new_text.",
            )
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        old_text = arguments.get("old_text")
        if not isinstance(old_text, str) or not old_text:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "old_text must be a non-empty string.")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "new_text must be a string.")
        if "\x00" in old_text or "\x00" in new_text:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "old_text and new_text must not contain NUL bytes.",
            )
        if old_text == new_text:
            return tool_error("NO_CHANGES", "old_text and new_text are identical.")

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = candidate.lstat()
            if S_ISLNK(info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            if info.st_size > self.max_content_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_content_bytes} bytes.",
                )
            with target.open("rb") as source:
                raw = source.read(self.max_content_bytes + 1)
            if len(raw) > self.max_content_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_content_bytes} bytes.",
                )
            if b"\x00" in raw:
                return tool_error(ToolErrorCode.BINARY_FILE)
            text = raw.decode("utf-8-sig")
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except UnicodeDecodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR)

        # Choose a newline convention for the replacement only. Existing text
        # outside the match must retain its original (possibly mixed) newlines.
        if "\r\n" in text:
            newline = "\r\n"
        elif "\r" in text:
            newline = "\r"
        else:
            newline = "\n"
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        # Agent-facing fragments use LF semantics, matching read_file output.
        old_normalized = old_text.replace("\r\n", "\n").replace("\r", "\n")
        new_normalized = new_text.replace("\r\n", "\n").replace("\r", "\n")
        match_start = normalized.find(old_normalized)
        if match_start == -1:
            return tool_error("NO_MATCH", "old_text does not occur in the file.")
        # Starting one character later also catches overlapping occurrences.
        if normalized.find(old_normalized, match_start + 1) != -1:
            return tool_error("MULTIPLE_MATCHES", "old_text occurs more than once in the file.")
        start_line = normalized.count("\n", 0, match_start) + 1
        original_start = self._original_text_offset(text, match_start)
        original_end = self._original_text_offset(text, match_start + len(old_normalized))
        replacement = new_normalized.replace("\n", newline)
        updated_text = text[:original_start] + replacement + text[original_end:]

        # utf-8-sig decoding stripped the BOM, so restore exactly the original one.
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        encoded = bom + updated_text.encode("utf-8")
        if len(encoded) > self.max_content_bytes:
            return tool_error(
                "EDIT_RESULT_TOO_LARGE",
                f"Edited content would exceed {self.max_content_bytes} bytes.",
            )
        old_hash = hashlib.sha256(raw).hexdigest()
        new_hash = hashlib.sha256(encoded).hexdigest()
        old_mode = info.st_mode & 0o777
        temp_path: Path | None = None

        try:
            with NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(encoded)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_path, old_mode)
            temp_path.replace(target)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.WRITE_ERROR)
        finally:
            if temp_path is not None:
                try:
                    # Successful replace already removed the temporary path.
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    # Cleanup failure must not replace the original write error.
                    logger.warning(
                        "Unable to remove temporary file %s", temp_path, exc_info=True
                    )

        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "replacements": 1,
                "start_line": start_line,
                "oldline_count": self._count_lines(old_normalized),
                "newline_count": self._count_lines(new_normalized),
                "bytes_before": len(raw),
                "bytes_after": len(encoded),
                "old_sha256": old_hash,
                "new_sha256": new_hash,
            },
        )

    @staticmethod
    def _original_text_offset(text: str, normalized_offset: int) -> int:
        """Map an LF-normalized boundary back to the original text boundary."""
        original_offset = normalized_offset
        cursor = 0
        while True:
            crlf = text.find("\r\n", cursor)
            if crlf == -1 or crlf >= original_offset:
                return original_offset
            # CRLF loses one character during normalization; lone CR does not.
            original_offset += 1
            cursor = crlf + 2

    @staticmethod
    def _count_lines(text: str) -> int:
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)

@dataclass(frozen=True)
class _PreparedEdit:
    index: int
    start: int
    end: int
    old_text: str
    new_text: str
    start_line: int

# BatchEditFileTool
class BatchEditFileTool:
    """Batched replace text fragments in an existing UTF-8 workspace file."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_edits: int = 32,
        max_content_bytes: int = 2 * 1024 * 1024,
        max_total_edit_bytes: int = 256 * 1024,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_edits", max_edits),
            ("max_content_bytes", max_content_bytes),
            ("max_total_edit_bytes", max_total_edit_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_edits = max_edits
        self.max_content_bytes = max_content_bytes
        self.max_total_edit_bytes = max_total_edit_bytes

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="batch_edit_file",
            description=(
                "Apply multiple exact replacements to one existing UTF-8 "
                "file inside the workspace. Every old_text must match "
                "exactly once in the original file, and edit ranges must "
                "not overlap. All edits are validated before the file is "
                "changed, then committed with one atomic replacement. "
                "new_text may be empty to delete matched text. "
                "Use expected_sha256 when editing content previously read "
                "to avoid overwriting a changed file."
                "Line breaks will be normalized. Currently, when the replacement "
                "fragment does not contain line breaks, the new line will default "
                "to using LF, which may introduce mixed line breaks to CRLF files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Path to an existing UTF-8 regular file "
                            "inside the workspace."
                        ),
                    },
                    "edits": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": self.max_edits,
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "Exact text that must occur exactly once in the original file."
                                    ),
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": (
                                        "Replacement text. May be empty."
                                    ),
                                },
                            },
                            "required": ["old_text", "new_text",],
                            "additionalProperties": False,
                        },
                    },
                    "expected_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-fA-F]{64}$",
                        "description": (
                            "Optional SHA-256 of the file bytes that "
                            "must match before editing."
                        ),
                    },
                },
                "required": ["path", "edits",],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "edits", "expected_sha256"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, edits, expected_sha256.",
            )
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits or len(edits) > self.max_edits:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"edits must be a non-empty list and contains less than {self.max_edits} items"
            )
        expected_sha256 = arguments.get("expected_sha256")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    "expected_sha256 must be a 64-character hexadecimal SHA-256 digest."
                )

        validated_edits: list[tuple[str, str]] = []
        total_edit_bytes = 0
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS, f"edit {index} must be an object."
                )
            if set(edit) - {"old_text", "new_text"}:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"edit {index} allows only old_text and new_text."
                )
            old_text = edit.get("old_text")
            if not isinstance(old_text, str) or not old_text:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS, f"old_text in edit {index} must be a non-empty string."
                )
            new_text = edit.get("new_text")
            if not isinstance(new_text, str):
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS, f"new_text in edit {index} must be a string."
                )
            if "\x00" in old_text or "\x00" in new_text:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"old_text and new_text in edit {index} must not contain NUL bytes.",
                )
            if old_text == new_text:
                return tool_error("NO_CHANGES", f"old_text and new_text in edit {index} are identical.")
            try:
                total_edit_bytes += len(old_text.encode("utf-8"))
                total_edit_bytes += len(new_text.encode("utf-8"))
            except UnicodeEncodeError:
                return tool_error(
                    ToolErrorCode.UNSUPPORTED_ENCODING, f"Text in edit {index} must use valid UTF-8 encoding."
                )
            if total_edit_bytes > self.max_total_edit_bytes:
                return tool_error(
                    "EDITS_TOO_LARGE", f"Combined edit text exceeds {self.max_total_edit_bytes} UTF-8 bytes."
                )
            validated_edits.append((old_text, new_text))

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = candidate.lstat()
            if S_ISLNK(info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            if info.st_size > self.max_content_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_content_bytes} bytes.",
                )
            with target.open("rb") as source:
                raw = source.read(self.max_content_bytes + 1)
            if len(raw) > self.max_content_bytes:
                return tool_error(
                    ToolErrorCode.FILE_TOO_LARGE,
                    f"File exceeds {self.max_content_bytes} bytes.",
                )
            if b"\x00" in raw:
                return tool_error(ToolErrorCode.BINARY_FILE)
            text = raw.decode("utf-8-sig")
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except UnicodeDecodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR)

        sha256_before = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and sha256_before != expected_sha256.lower():
            return tool_error(ToolErrorCode.FILE_CHANGED)

        prepared_edits: list[_PreparedEdit] = []
        seen_old_text: set[str] = set()
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for index, (raw_old_text, raw_new_text) in enumerate(validated_edits, start=1):
            old_text = raw_old_text.replace("\r\n", "\n").replace("\r", "\n")
            new_text = raw_new_text.replace("\r\n", "\n").replace("\r", "\n")
            if old_text in seen_old_text:
                return tool_error(
                    "DUPLICATE_EDIT", f"edit {index} repeats an old_text already used by another edit."
                )
            seen_old_text.add(old_text)
            start = normalized.find(old_text)
            if start < 0:
                return tool_error(
                    "NO_MATCH", f"edit {index}.old_text was not found in the original file."
                )
            second = normalized.find(old_text, start + 1)
            if second >= 0:
                return tool_error(
                    "MULTIPLE_MATCHES", f"edit {index}.old_text occurs more than once."
                )
            end = start + len(old_text)
            start_line = normalized.count("\n", 0, start) + 1
            prepared_edits.append(
                _PreparedEdit(
                    index=index,
                    start=start,
                    end=end,
                    old_text=old_text,
                    new_text=new_text,
                    start_line=start_line,
                )
            )
        prepared_edits.sort(key=lambda edit: edit.start)
        for previous, current in zip(prepared_edits, prepared_edits[1:]):
            if current.start < previous.end:
                return tool_error(
                    "OVERLAPPING_EDITS", f"edit {previous.index} and edit {current.index} overlap in the original file."
                )

        normalized_offset = [(current_edit.start, current_edit.end) for current_edit in prepared_edits]
        original_text_offset = self._get_original_text_offset(text, normalized_offset)
        slice_start, slice_end = 0, 0
        updated_text_fragments: list[str] = []
        for index, current_edit in enumerate(prepared_edits):
            original_start, original_end = original_text_offset[index]
            if "\r\n" in text[original_start: original_end]:
                newline = "\r\n"
            elif "\r" in text[original_start: original_end]:
                newline = "\r"
            else:
                newline = "\n"
            slice_end = original_start
            new_normalized = current_edit.new_text.replace("\r\n", "\n").replace("\r", "\n")
            replacement = new_normalized.replace("\n", newline)
            updated_text_fragments.append(text[slice_start: slice_end])
            updated_text_fragments.append(replacement)
            slice_start = original_end
        if slice_start < len(text):
            updated_text_fragments.append(text[slice_start:])
        updated_text = "".join(updated_text_fragments)

        # utf-8-sig decoding stripped the BOM, so restore exactly the original one.
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        encoded = bom + updated_text.encode("utf-8")
        if len(encoded) > self.max_content_bytes:
            return tool_error(
                "EDIT_RESULT_TOO_LARGE",
                f"Edited content would exceed {self.max_content_bytes} bytes.",
            )
        old_hash = sha256_before
        new_hash = hashlib.sha256(encoded).hexdigest()
        old_mode = info.st_mode & 0o777
        temp_path: Path | None = None

        try:
            with NamedTemporaryFile(mode="wb", dir=target.parent, delete=False) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(encoded)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.chmod(temp_path, old_mode)
            temp_path.replace(target)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.WRITE_ERROR)
        finally:
            if temp_path is not None:
                try:
                    # Successful replace already removed the temporary path.
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    # Cleanup failure must not replace the original write error.
                    logger.warning(
                        "Unable to remove temporary file %s", temp_path, exc_info=True
                    )
        changes = [
            {
                "edit_index": edit.index,
                "start_line": edit.start_line,
                "old_line_count": (
                    self._count_fragment_lines(edit.old_text)
                ),
                "new_line_count": (
                    self._count_fragment_lines(edit.new_text)
                ),
            }
            for edit in prepared_edits
        ]
        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "edits_applied": len(prepared_edits),
                "changes": changes,
                "bytes_before": len(raw),
                "bytes_after": len(encoded),
                "old_sha256": old_hash,
                "new_sha256": new_hash,
            },
        )

    @staticmethod
    def _get_original_text_offset(text: str, normalized_offset: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Map an LF-normalized boundary back to the original text boundary."""
        item_num = len(normalized_offset)
        unfolded_original_offset: list[int] = []
        for offset in normalized_offset:
            unfolded_original_offset.append(offset[0])
            unfolded_original_offset.append(offset[1])
        original_offset: list[tuple[int, int]] = []
        cursor = 0
        index = 0
        accumulate_offset = 0
        while index < len(unfolded_original_offset):
            crlf = text.find("\r\n", cursor)
            if crlf == -1:
                break
            while (
                index < len(unfolded_original_offset)
                and unfolded_original_offset[index] <= crlf - accumulate_offset
            ):
                unfolded_original_offset[index] += accumulate_offset
                index += 1
            accumulate_offset += 1
            cursor = crlf + 2
        if index < len(unfolded_original_offset):
            for i in range(index, len(unfolded_original_offset)):
                unfolded_original_offset[i] += accumulate_offset
        for i in range(item_num):
            original_offset.append((unfolded_original_offset[i * 2], unfolded_original_offset[i * 2 + 1]))
        return original_offset

    @staticmethod
    def _count_fragment_lines(text: str,) -> int:
        if not text:
            return 0
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.count("\n") + (0 if normalized.endswith("\n") else 1)

    @staticmethod
    def _count_lines(text: str) -> int:
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)

# ListFileTool
class ListFileTool:
    """List files in a directory with optional filtering and recursion."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_entries: int = 200,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_entries", max_entries),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_entries = max_entries

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_files",
            description=(
                "List the direct children of a directory inside the workspace. "
                "Prefer workspace-relative paths and use '.' for the workspace root. "
                "The listing is non-recursive. Directories are listed before files. "
                f"Returns at most {self.max_entries} entries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to a directory inside the workspace. Use '.' for the workspace root.",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to include entries whose names start with '.'."
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "include_hidden"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, include_hidden.",
            )
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL.",
            )
        include_hidden = arguments.get("include_hidden", True)
        if not isinstance(include_hidden, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "include_hidden must be a boolean.")

        try:
            target = (self.workspace_root / path).resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
            entries: list[dict[str, Any]] = []
            for entry in target.iterdir():
                if not include_hidden and entry.name.startswith("."):
                    continue
                try:
                    if is_credential_path(entry, entry.resolve()):
                        continue
                    if entry.is_symlink():
                        entry_type = "symlink"
                        size = None
                    elif entry.is_dir():
                        entry_type = "directory"
                        size = None
                    elif entry.is_file():
                        entry_type = "file"
                        try:
                            size = entry.stat().st_size
                        except OSError:
                            size = None
                    else:
                        entry_type = "other"
                        size = None
                except OSError:
                    entry_type = "other"
                    size = None
                relative = entry.relative_to(self.workspace_root).as_posix()
                item = {
                    "name": entry.name,
                    "path": relative,
                    "type": entry_type,
                    }
                if size is not None:
                    item["size"] = size
                entries.append(item)
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR)
        
        type_order = {
            "directory": 0,
            "file": 1,
            "symlink": 2,
            "other": 3,
        }

        entries.sort(key=lambda x: (type_order[x["type"]], x["name"].casefold(), x["name"]))
        total_entries = len(entries)
        truncated = total_entries > self.max_entries
        entries = entries[: self.max_entries]

        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "entries": entries,
                "total_entries": total_entries,
                "returned_entries": len(entries),
                "truncated": truncated,
            },
        )

# FindFileTool
class FindFileTool:
    """Find files or directories by glob pattern inside the workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_results: int = 200,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_entries", max_results),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_results = max_results

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_files",
            description=(
                "Find files or directories recursively inside the workspace using a glob pattern. "
                "Use this when you know a file or directory name/pattern but do not know its location. "
                "The pattern is matched relative to the given search path. "
                "Absolute patterns and '..' path segments are rejected. "
                "Credential paths are excluded from results and counts, even with "
                "include_hidden=true; protected search roots are rejected. "
                "Examples: '**/pyproject.toml', '**/*.py', 'src/**/test_*.py'. "
                "Do not use this tool to search file contents; use search_files instead. "
                f"Returns at most {self.max_results} matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Glob pattern relative to the search path. "
                            "Must not be absolute or contain '..' path segments. "
                            "Examples: '**/config.py', '**/*.py', 'src/**/test_*.py'."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": (
                            "Directory inside the workspace to search from. "
                            "Use '.' for the workspace root."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "enum": ["file", "directory", "any"],
                        "default": "any",
                        "description": "Restrict matches by entry type.",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Whether matches inside hidden files/directories are allowed."
                        ),
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"pattern", "path", "type", "include_hidden"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: pattern, path, type, include_hidden.",
            )
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip() or "\x00" in pattern:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "pattern must be a non-empty string without NUL.",
            )
        pattern_path = Path(pattern)
        if pattern_path.anchor or ".." in pattern_path.parts:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "pattern must be relative to the search path without '..' path segments.",
            )
        path = arguments.get("path", ".")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL.",
            )
        entry_type = arguments.get("type", "any")
        if not isinstance(entry_type, str) or entry_type not in {"file", "directory", "any"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "type must be one of: file, directory, any.",
            )
        include_hidden = arguments.get("include_hidden", True)
        if not isinstance(include_hidden, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "include_hidden must be a boolean.")

        try:
            abs_path = self.workspace_root / path
            target = abs_path.resolve()
            if is_credential_path(abs_path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
            matches: list[dict[str, Any]] = []
            total_matches = 0
            for candidate in target.glob(pattern):
                try:
                    resolved = candidate.resolve()
                    if is_credential_path(candidate, resolved):
                        continue
                    if not resolved.is_relative_to(self.workspace_root):
                        continue
                    relative_to_search = candidate.relative_to(target)
                    if not include_hidden:
                        if any(part.startswith(".") for part in relative_to_search.parts):
                            continue
                    if candidate.is_symlink():
                        candidate_type = "symlink"
                    elif candidate.is_dir():
                        candidate_type = "directory"
                    elif candidate.is_file():
                        candidate_type = "file"
                    else:
                        candidate_type = "other"
                    if entry_type == "file" and candidate_type != "file":
                        continue
                    if entry_type == "directory" and candidate_type != "directory":
                        continue
                    total_matches += 1
                    if total_matches > self.max_results:
                        continue
                    relative = candidate.relative_to(self.workspace_root).as_posix()
                    item: dict[str, Any] = {
                        "name": candidate.name,
                        "path": relative,
                        "type": candidate_type,
                    }
                    if candidate_type == "file":
                        try:
                            item["size"] = candidate.stat().st_size
                        except OSError:
                            pass
                    matches.append(item)
                except (OSError, RuntimeError):
                    continue
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError, ValueError):
            return tool_error(ToolErrorCode.READ_ERROR)
        
        type_order = {
            "directory": 0,
            "file": 1,
            "symlink": 2,
            "other": 3,
        }

        matches.sort(key=lambda x: (type_order[x["type"]], x["path"].casefold(), x["path"]))
        truncated = total_matches > len(matches)

        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "pattern": pattern,
                "matches": matches,
                "total_matches": total_matches,
                "returned_matches": len(matches),
                "truncated": truncated,
            },
        )

# SearchFilesTool
class SearchFilesTool:
    """Search for files in a directory with optional filtering and recursion."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_results: int = 100,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_line_chars: int = 2000,
        max_files_scanned: int = 10_000,
        max_output_chars: int = 20_000,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in (
            ("max_results", max_results),
            ("max_file_bytes", max_file_bytes),
            ("max_line_chars", max_line_chars),
            ("max_files_scanned", max_files_scanned),
            ("max_output_chars", max_output_chars)
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_results = max_results
        self.max_file_bytes = max_file_bytes
        self.max_line_chars = max_line_chars
        self.max_files_scanned = max_files_scanned
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_files",
            description=(
                "Search UTF-8 text files inside the workspace for a literal text substring. "
                "Directories are searched recursively. Binary, oversized, unreadable, "
                "and unsupported-encoding files are skipped. "
                "Credential files and directories are always excluded, even with include_hidden=true. "
                "Returns workspace-relative paths, line numbers, and matching lines. "
                f"Returns at most {self.max_results} matching lines."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Literal text to search for.",
                    },
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "default": ".",
                        "description": (
                            "Directory or file inside the workspace to search. "
                            "Use '.' for the workspace root."
                        ),
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether matching is case-sensitive.",
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Whether to include hidden files and files inside hidden directories, "
                            "including an explicitly selected hidden path. Defaults to false."
                        ),
                    },
                    "glob": {
                        "type": "string",
                        "description": (
                            "Optional filename glob used to restrict searched files, "
                            "for example '*.py' or 'tests/test_*.py'. "
                            "Matches the full workspace-relative path with '/' separators, "
                            "even when path selects a subdirectory or file. '*' can match '/'."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"query", "path", "case_sensitive", "include_hidden", "glob"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: query, path, case_sensitive, include_hidden, glob.",
            )
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "query must be a non-empty string.")
        if "\x00" in query:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "query must not contain NUL bytes.")
        path = arguments.get("path", ".")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        case_sensitive = arguments.get("case_sensitive", True)
        if not isinstance(case_sensitive, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "case_sensitive must be a boolean.")
        include_hidden = arguments.get("include_hidden", False)
        if not isinstance(include_hidden, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "include_hidden must be a boolean.")
        glob = arguments.get("glob")
        if glob is not None:
            if not isinstance(glob, str):
                return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "glob must be a string.")
            if "\x00" in glob:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    "glob must not contain NUL bytes.",
                )

        try:
            requested = self.workspace_root / path
            target = requested.resolve()
            if is_credential_path(requested, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target.exists():
                return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error("SEARCH_ERROR", "Unable to resolve the search path.")
        
        matches = list[dict[str, Any]]()
        files_scanned = 0
        skipped_files = 0
        files_with_matches = 0
        output_chars = 0
        truncated = False
        truncation_reason = None
        if case_sensitive:
            def find_match_index(line: str) -> int:
                return line.find(query)
        else:
            query_lower = query.casefold()
            def find_match_index(line: str) -> int:
                return line.casefold().find(query_lower)

        try:
            files = self._iter_files(target, include_hidden=include_hidden, glob=glob)
            for file in files:
                if files_scanned >= self.max_files_scanned:
                    truncated = True
                    truncation_reason = "max_files_scanned"
                    break
                files_scanned += 1
                try:
                    resolved = file.resolve()
                    if (
                        not resolved.is_relative_to(self.workspace_root)
                        or is_credential_path(file, resolved)
                    ):
                        skipped_files += 1
                        continue
                    info = file.stat()
                    if not S_ISREG(info.st_mode) or info.st_size > self.max_file_bytes:
                        skipped_files += 1
                        continue
                    with file.open("rb") as source:
                        raw = source.read(self.max_file_bytes + 1)
                    if len(raw) > self.max_file_bytes or b"\x00" in raw:
                        skipped_files += 1
                        continue
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        skipped_files += 1
                        continue
                except (PermissionError, OSError, RuntimeError):
                    skipped_files += 1
                    continue
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                file_matched = False
                for line_number, line in enumerate(normalized.split("\n"), start=1):
                    match_index = find_match_index(line)
                    if match_index == -1:
                        continue
                    if len(matches) >= self.max_results:
                        truncated = True
                        truncation_reason = "max_results"
                        break
                    if not file_matched:
                        files_with_matches += 1
                        file_matched = True
                    truncated_lines = self._truncate_matching_line(line, match_index, len(query))
                    output_chars += len(truncated_lines)
                    if output_chars > self.max_output_chars:
                        truncated = True
                        truncation_reason = "max_output_chars"
                        break
                    matches.append(
                        {
                            "path": file.relative_to(self.workspace_root).as_posix(),
                            "line_number": line_number,
                            "line": truncated_lines,
                        }
                    )
                if truncated:
                    break
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error("SEARCH_ERROR", "Unable to search the path.")

        return ToolResult(
            success=True,
            data={
                "query": query,
                "path": target.relative_to(self.workspace_root).as_posix(),
                "matches": matches,
                "matches_returned": len(matches),
                "files_scanned": files_scanned,
                "files_with_matches": files_with_matches,
                "skipped_files": skipped_files,
                "truncated": truncated,
                "truncation_reason": truncation_reason,
            },
        )

    def _iter_files(
        self,
        target: Path,
        *,
        include_hidden: bool,
        glob: str | None,
    ):
        # Apply the hidden-directory rule to explicit paths as well as recursion.
        relative = target.relative_to(self.workspace_root)
        if not include_hidden and any(part.startswith(".") for part in relative.parts):
            return
        if target.is_file():
            if glob is None or fnmatch(relative.as_posix(), glob):
                yield target
            return
        if not target.is_dir():
            return
        for root, dirnames, filenames in os.walk(target, followlinks=False):
            dirnames.sort(key=lambda x: (x.casefold(), x))
            filenames.sort(key=lambda x: (x.casefold(), x))
            root_path = Path(root)
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                filenames = [f for f in filenames if not f.startswith(".")]
            # Prune protected directories before os.walk can descend into them.
            dirnames[:] = [
                name for name in dirnames
                if not is_credential_path(root_path / name, (root_path / name).resolve())
            ]
            for file in filenames:
                candidate = root_path / file
                try:
                    if candidate.is_symlink():
                        continue
                except OSError:
                    continue
                relative = candidate.relative_to(self.workspace_root).as_posix()
                if glob is None or fnmatch(relative, glob):
                    yield candidate

    def _truncate_matching_line(
        self,
        line: str,
        match_index: int,
        match_length: int,
    ) -> str:
        if len(line) <= self.max_line_chars:
            return line
        available = self.max_line_chars - match_length
        if available <= 0:
            return line[match_index: match_index + self.max_line_chars]
        before = available // 2
        after = available - before
        start = max(0, match_index - before)
        end = min(len(line), match_index + match_length + after)
        fragment = line[start:end]
        if start > 0:
            fragment = "..." + fragment
        if end < len(line):
            fragment = fragment + "..."
        return fragment


# MakeDirectoryTool
class MakeDirectoryTool:
    """Create directories inside a bounded workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="make_directory",
            description=(
                "Create a directory inside the workspace. "
                "Prefer workspace-relative paths. "
                "Protected credential paths and directories are rejected, even if they exist. "
                "If the directory already exists, the operation succeeds "
                "with created=false. Missing parent directories are created "
                "only when parents=true. When failing, "
                "it may leave the parent directories that have been created."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to a directory inside the workspace. Use '.' for the workspace root.",
                    },
                    "parents": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to create missing parent directories."
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "parents"}:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "Allowed arguments: path, parents.")
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        parents = arguments.get("parents", False)
        if not isinstance(parents, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "parents must be a boolean.")

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(candidate, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if candidate.is_symlink():
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if target.exists():
                if not target.is_dir():
                    return tool_error(ToolErrorCode.PATH_ALREADY_EXISTS)
                return ToolResult(
                    success=True,
                    data={
                        "path": target.relative_to(self.workspace_root).as_posix(),
                        "created": False,
                    },
                )
            if not parents and not target.parent.is_dir():
                return tool_error(ToolErrorCode.PARENT_NOT_FOUND)
            target.mkdir(parents=parents, exist_ok=False)
        except FileExistsError:
            try:
                if target.is_dir():
                    return ToolResult(
                        success=True,
                        data={
                            "path": target.relative_to(self.workspace_root).as_posix(),
                            "created": False,
                        },
                    )
            except OSError:
                pass
            return tool_error(ToolErrorCode.PATH_ALREADY_EXISTS)
        except FileNotFoundError:
            return tool_error(ToolErrorCode.PARENT_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error("CREATE_DIRECTORY_ERROR", "Unable to create the directory.")
    
        return ToolResult(
            success=True,
            data={
                "path": target.relative_to(self.workspace_root).as_posix(),
                "created": True,
            },
        )


# DeleteFileTool
class DeleteFileTool:
    """Delete a file inside a bounded workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="delete_file",
            description=(
                "Delete one existing regular file inside the workspace. "
                "Directories, symbolic links, and special files are not supported. "
                "Prefer workspace-relative paths. "
                "Use this tool only when the file should actually be removed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to an existing regular file inside the workspace.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path"}:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "Allowed arguments: path.")
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(candidate, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = candidate.lstat()
            if S_ISLNK(info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            relative_path = target.relative_to(self.workspace_root).as_posix()
            bytes_deleted = info.st_size
            target.unlink()
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except IsADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_FILE)
        except (OSError, RuntimeError):
            return tool_error("DELETE_FILE_ERROR", "Unable to delete the file.")

        return ToolResult(
            success=True,
            data={
                "path": relative_path,
                "deleted": True,
                "bytes_deleted": bytes_deleted,
            },
        )

# MoveFileTool
class MoveFileTool:
    """Move or rename one regular file inside a bounded workspace."""

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="move_file",
            description=(
                "Move or rename one existing regular file inside the workspace. "
                "Symbolic links, directories, and special files are not supported. "
                "The destination must not already exist. "
                "Missing destination parent directories are created only when "
                "create_parents=true. When 'create_parents' is set to 'true' "
                "and the file movement fails, the created directory will still remain."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Workspace-relative path of the existing file."
                        ),
                    },
                    "destination": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Workspace-relative destination path."
                        ),
                    },
                    "create_parents": {
                        "type": "boolean",
                        "default": False,
                    },
                },
                "required": ["source", "destination"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"source", "destination", "create_parents"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, 
                "Allowed arguments: source, destination, create_parents"
            )
        source = arguments.get("source")
        if not isinstance(source, str) or not source.strip() or "\x00" in source:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "source must be a non-empty string without NUL.")
        destination = arguments.get("destination")
        if not isinstance(destination, str) or not destination.strip() or "\x00" in destination:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "destination must be a non-empty string without NUL.")
        create_parents = arguments.get("create_parents", False)
        if not isinstance(create_parents, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "create_parents must be a boolean.") 

        try:
            source_candidate = self.workspace_root / source
            destination_candidate = self.workspace_root / destination
            source_target = source_candidate.resolve()
            destination_target = destination_candidate.resolve()
            if (is_credential_path(source_candidate, source_target) 
                or is_credential_path(destination_candidate, destination_target)):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not source_target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "source must stay inside the workspace.")
            if not destination_target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "denstination must stay inside the workspace.")
            source_info = source_candidate.lstat()
            if S_ISLNK(source_info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK, "Moving symbolic links is not supported.")
            if not S_ISREG(source_info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE, "source must refer to a regular file.")
            try:
                destination_candidate.lstat()
            except FileNotFoundError:
                pass
            else:
                return tool_error("DESTINATION_ALREADY_EXISTS", "The destination path already exists.")
            destination_parent = destination_candidate.parent.resolve()
            if not destination_parent.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "The destination parent must stay inside the workspace.")
            if not destination_parent.exists():
                if not create_parents:
                    return tool_error(ToolErrorCode.PARENT_NOT_FOUND, "The destination parent directory does not exist.")
                destination_parent.mkdir(parents=True, exist_ok=True)
            if not destination_parent.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY, "The destination parent must be a directory.")
            source_relative = source_target.relative_to(self.workspace_root).as_posix()
            destination_relative = destination_target.relative_to(self.workspace_root).as_posix()
            bytes_moved = source_info.st_size
            source_candidate.rename(destination_candidate)
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND, "The source file does not exist.")
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED, "The file cannot be moved with current permissions.")
        except RuntimeError:
            return tool_error("MOVE_ERROR", "Unable to move the file.",)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return tool_error(
                    "CROSS_DEVICE_MOVE_NOT_SUPPORTED",
                    "Moving files across filesystems is not supported.",
                )
            return tool_error("MOVE_ERROR", "Unable to move the file.",)

        return ToolResult(
            success=True,
            data={
                "source": source_relative,
                "destination": destination_relative,
                "moved": True,
                "bytes_moved": bytes_moved,
            },
        )

# GetPathInfoTool
class GetPathInfoTool:
    """Inspect filesystem metadata for one workspace path."""

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_path_info",
            description=(
                "Inspect filesystem metadata for one path inside the workspace. "
                "Reports whether the path is a regular file, directory, symbolic "
                "link, or other filesystem object. Metadata describes the link itself, "
                "but its target is resolved for workspace and credential checks. "
                "Links outside the workspace or to credential paths are rejected; "
                "unresolvable link cycles return STAT_ERROR. The returned path identifies "
                "the inspected entry, preserving its final link name. Missing paths return "
                "FILE_NOT_FOUND. executable reports an access check, not a guarantee "
                "that a program will run successfully."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Path inside the workspace to inspect."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path"}:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "Allowed arguments: path.")
        path = arguments.get("path")
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(candidate, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = candidate.lstat()
            if S_ISLNK(info.st_mode):
                path_type = "symlink"
                # Resolve parent aliases, but retain the final link's own name.
                inspected_path = candidate.parent.resolve() / candidate.name
                if not inspected_path.is_relative_to(self.workspace_root):
                    return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            elif S_ISREG(info.st_mode):
                path_type = "file"
            elif S_ISDIR(info.st_mode):
                path_type = "directory"
            else:
                path_type = "other"
            if path_type != "symlink":
                inspected_path = target
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND)
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(
                ToolErrorCode.PERMISSION_DENIED, 
                "The path cannot be inspected with current permissions."
            )
        except (OSError, RuntimeError):
            return tool_error("STAT_ERROR", "Unable to inspect the path.")
        
        data: dict[str, Any] = {
            "path": inspected_path.relative_to(self.workspace_root).as_posix(),
            "type": path_type,
        }
        if path_type == "file":
            data["size"] = info.st_size
            try:
                data["executable"] = os.access(candidate, os.X_OK,)
            except OSError:
                data["executable"] = False
        return ToolResult(
            success=True,
            data=data
        )
