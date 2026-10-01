"""
Workspace-scoped file operations. This path guard is not an OS sandbox.

Includes tools related to file operations:
ReadFileTool: Read UTF-8 text files with line ranges and bounded output.
WriteFileTool: Create or replace UTF-8 text files with bounded content.
EditFileTool: replace one or multiple text fragments in an existing UTF-8 workspace file.
ApplyPatchTool: Apply strict context-based patches to existing UTF-8 workspace files
ListFilesTool: List files in a directory with optional filtering and recursion.
SearchFilesTool: Search for a text fragment in files with optional filtering and recursion.
MakeDirectoryTool: Create a directory and optionally its parents.
DeleteFileTool: Delete a file, ensuring it is not a directory or symlink.
MoveFileTool: Move or rename one regular file inside a bounded workspace.
GetPathInfoTool: Inspect filesystem metadata for one workspace path.

All tools enforce workspace-relative paths and prevent access to credential files.
"""

import errno
import hashlib
import logging
import os
import re
from contextlib import closing
from dataclasses import dataclass
from functools import wraps
from itertools import pairwise
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from llm import ToolDefinition

from ._internal._file_entries import inspect_entry, iter_search_candidates
from ._internal._file_io import FileSnapshot, StagedWrites, read_snapshot, snapshot_stat
from ._internal._workspace import WorkspaceTool
from ._internal._workspace import serialized_file_write as _serialized_file_write
from ._internal.base import ExecutionKind, ToolResult
from ._internal.errors import ToolErrorCode, tool_error
from ._internal.file_access import FileAccess, current_file_access
from ._internal.file_policy import is_credential_path
from ._internal.text_search import text_lines

logger = logging.getLogger(__name__)


class FileTool(WorkspaceTool):
    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        method = cls.__dict__.get("execute")
        if method is None:
            return

        @wraps(method)
        def execute(self, arguments):
            if os.name != "nt" or current_file_access() is not None:
                return method(self, arguments)
            try:
                with FileAccess(self.workspace_root).activate():
                    return method(self, arguments)
            except PermissionError:
                return tool_error(ToolErrorCode.PERMISSION_DENIED)
            except OSError:
                return tool_error(ToolErrorCode.READ_ERROR)
            except ValueError as error:
                return tool_error(ToolErrorCode.INVALID_ARGUMENTS, str(error))

        cls.execute = execute

    @staticmethod
    def _mkdir(path, **options):
        access = current_file_access()
        if access:
            return access.mkdir(path, **options)
        return path.mkdir(**options)


# ReadFileTool
class ReadFileTool(FileTool):
    """Read one or multiple UTF-8 source files.

    Use 1-based inclusive line ranges and bounded output.
    """

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_reads: int = 32,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_output_chars: int = 256 * 1024,
    ) -> None:
        super().__init__(
            workspace_root,
            max_reads=max_reads,
            max_file_bytes=max_file_bytes,
            max_output_chars=max_output_chars,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="read_file",
            description=(
                "Read one or multiple UTF-8 text files inside the workspace. "
                "Prefer workspace-relative paths. Pass reads as an array, even for one file. "
                "Each read has its own path and optional 1-based inclusive start_line/end_line. "
                "Results preserve request order, including duplicates. "
                "Each result has its own success, data and optional error; "
                "a file error does not stop other reads. "
                "Overall success is false if any read fails; "
                "successful results are still returned. "
                f"Returns at most {self.max_output_chars} numbered-content characters "
                "across all files, "
                "allocated in request order. Metadata is not included in this limit. "
                "Only complete lines are returned; "
                "use each result's next_start_line to continue truncated reads. "
                "OUTPUT_TOO_LARGE means no requested line fits; "
                "retry that file separately or request fewer lines. "
                "A line exceeding the entire budget requires increasing "
                "the host's max_output_chars setting."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "reads": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": self.max_reads,
                        "items": {
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
                    },
                },
                "required": ["reads"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate the whole request before reading; isolate individual file failures."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        reads = arguments.get("reads")
        if (
            set(arguments) != {"reads"}
            or not isinstance(reads, list)
            or not 1 <= len(reads) <= self.max_reads
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"Provide only reads: an array of 1 to {self.max_reads} file requests.",
            )
        for index, read in enumerate(reads):
            if not isinstance(read, dict) or set(read) - {"path", "start_line", "end_line"}:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"reads[{index}] allows only path, start_line, end_line.",
                )
            path = read.get("path")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"reads[{index}].path must be a non-empty string without NUL.",
                )
            start = read.get("start_line", 1)
            end = read.get("end_line")
            if type(start) is not int or start < 1:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"reads[{index}].start_line must be a positive integer.",
                )
            if "end_line" in read and (type(end) is not int or end < start):
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"reads[{index}].end_line must be an integer >= start_line.",
                )

        results = []
        total_output_chars = 0
        for read in reads:
            result = self._read_one(read, self.max_output_chars - total_output_chars)
            entry = {"path": read["path"], "success": result.success, "data": result.data}
            if not result.success:
                entry["error"] = {"code": result.error_code, "message": result.error}
            total_output_chars += len(result.data.get("content", ""))
            results.append(entry)
        failures = sum(not entry["success"] for entry in results)
        return ToolResult(
            success=failures == 0,
            data={"results": results, "total_output_chars": total_output_chars},
            error_code="READ_FAILED" if failures else None,
            error=(
                f"{failures} of {len(reads)} reads failed; "
                "inspect results for per-file errors and successful content."
                if failures
                else None
            ),
        )

    def _read_one(self, arguments: dict[str, Any], output_budget: int) -> ToolResult:
        path = arguments["path"]
        start = arguments.get("start_line", 1)
        end = arguments.get("end_line")

        try:
            target = (self.workspace_root / path).resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = snapshot_stat(target, follow_symlinks=True)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            snapshot = read_snapshot(
                target,
                target,
                info,
                self.max_file_bytes,
                size_message=f"File exceeds {self.max_file_bytes} bytes.",
            )
            if isinstance(snapshot, ToolResult):
                return snapshot
            raw = snapshot.raw
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
        numbered_lines = []
        content_chars = 0
        actual_end = start - 1
        for number in range(start, requested_end + 1):
            line = f"{number}: {lines[number - 1]}"
            required_chars = len(line) + bool(numbered_lines)
            if content_chars + required_chars > output_budget:
                break
            numbered_lines.append(line)
            content_chars += required_chars
            actual_end = number
        if total and not numbered_lines:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE,
                "The first requested numbered line does not fit the remaining "
                f"{output_budget} characters. "
                "Retry this file separately; a line exceeding the full budget requires increasing "
                "the host's max_output_chars setting.",
            )
        content = "\n".join(numbered_lines)
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
                "sha256": snapshot.sha256,
            },
        )


# WriteFileTool
class WriteFileTool(FileTool):
    """Create or replace bounded UTF-8 text files inside a workspace."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_content_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        super().__init__(
            workspace_root,
            max_content_bytes=max_content_bytes,
        )

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
                        "description": (
                            "Whether to overwrite an existing file. "
                            "If false and the file exists, the tool returns an error."
                        ),
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

    @_serialized_file_write
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
                    self._mkdir(parent, parents=True, exist_ok=True)
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
                old_mode = info.st_mode

            with StagedWrites(temp_factory=NamedTemporaryFile, logger=logger) as staged:
                staged.replace(target, encoded, old_mode if existed else None)

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


@dataclass(frozen=True)
class _PreparedEdit:
    index: int
    start: int
    end: int
    old_text: str
    new_text: str
    start_line: int


# EditFileTool
class EditFileTool(FileTool):
    """replace one or multiple text fragments in an existing UTF-8 workspace file."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_edits: int = 32,
        max_content_bytes: int = 2 * 1024 * 1024,
        max_total_edit_bytes: int = 256 * 1024,
    ) -> None:
        super().__init__(
            workspace_root,
            max_edits=max_edits,
            max_content_bytes=max_content_bytes,
            max_total_edit_bytes=max_total_edit_bytes,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="edit_file",
            description=(
                "Apply one or multiple exact replacements to one existing UTF-8 "
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
                            "Path to an existing UTF-8 regular file inside the workspace."
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
                                        "Exact text that must occur exactly once "
                                        "in the original file."
                                    ),
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": ("Replacement text. May be empty."),
                                },
                            },
                            "required": [
                                "old_text",
                                "new_text",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "expected_sha256": {
                        "type": "string",
                        "pattern": "^[0-9a-fA-F]{64}$",
                        "description": (
                            "Optional SHA-256 of the file bytes that must match before editing."
                        ),
                    },
                },
                "required": [
                    "path",
                    "edits",
                ],
                "additionalProperties": False,
            },
        )

    @_serialized_file_write
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
                f"edits must be a non-empty list and contains less than {self.max_edits} items",
            )
        expected_sha256 = arguments.get("expected_sha256")
        if expected_sha256 is not None:
            if (
                not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None
            ):
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    "expected_sha256 must be a 64-character hexadecimal SHA-256 digest.",
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
                    f"edit {index} allows only old_text and new_text.",
                )
            old_text = edit.get("old_text")
            if not isinstance(old_text, str) or not old_text:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"old_text in edit {index} must be a non-empty string.",
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
                return tool_error(
                    "NO_CHANGES", f"old_text and new_text in edit {index} are identical."
                )
            try:
                total_edit_bytes += len(old_text.encode("utf-8"))
                total_edit_bytes += len(new_text.encode("utf-8"))
            except UnicodeEncodeError:
                return tool_error(
                    ToolErrorCode.UNSUPPORTED_ENCODING,
                    f"Text in edit {index} must use valid UTF-8 encoding.",
                )
            if total_edit_bytes > self.max_total_edit_bytes:
                return tool_error(
                    "EDITS_TOO_LARGE",
                    f"Combined edit text exceeds {self.max_total_edit_bytes} UTF-8 bytes.",
                )
            validated_edits.append((old_text, new_text))

        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = snapshot_stat(candidate)
            if S_ISLNK(info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            snapshot = read_snapshot(
                target,
                target,
                info,
                self.max_content_bytes,
                size_message=f"File exceeds {self.max_content_bytes} bytes.",
            )
            if isinstance(snapshot, ToolResult):
                return snapshot
            raw = snapshot.raw
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

        sha256_before = snapshot.sha256
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
                    "DUPLICATE_EDIT",
                    f"edit {index} repeats an old_text already used by another edit.",
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
        for previous, current in pairwise(prepared_edits):
            if current.start < previous.end:
                return tool_error(
                    "OVERLAPPING_EDITS",
                    f"edit {previous.index} and edit {current.index} overlap in the original file.",
                )

        normalized_offset = [
            (current_edit.start, current_edit.end) for current_edit in prepared_edits
        ]
        original_text_offset = self._get_original_text_offset(text, normalized_offset)
        slice_start, slice_end = 0, 0
        updated_text_fragments: list[str] = []
        for index, current_edit in enumerate(prepared_edits):
            original_start, original_end = original_text_offset[index]
            if "\r\n" in text[original_start:original_end]:
                newline = "\r\n"
            elif "\r" in text[original_start:original_end]:
                newline = "\r"
            else:
                newline = "\n"
            slice_end = original_start
            new_normalized = current_edit.new_text.replace("\r\n", "\n").replace("\r", "\n")
            replacement = new_normalized.replace("\n", newline)
            updated_text_fragments.append(text[slice_start:slice_end])
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
        try:
            with StagedWrites(temp_factory=NamedTemporaryFile, logger=logger) as staged:
                staged.replace(target, encoded, old_mode)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.WRITE_ERROR)
        changes = [
            {
                "edit_index": edit.index,
                "start_line": edit.start_line,
                "old_line_count": (self._count_fragment_lines(edit.old_text)),
                "new_line_count": (self._count_fragment_lines(edit.new_text)),
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
    def _get_original_text_offset(
        text: str, normalized_offset: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
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
            original_offset.append(
                (unfolded_original_offset[i * 2], unfolded_original_offset[i * 2 + 1])
            )
        return original_offset

    @staticmethod
    def _count_fragment_lines(
        text: str,
    ) -> int:
        if not text:
            return 0
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.count("\n") + (0 if normalized.endswith("\n") else 1)

    @staticmethod
    def _count_lines(text: str) -> int:
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)


@dataclass(frozen=True)
class _PatchLine:
    kind: Literal["context", "add", "remove"]
    text: str


@dataclass(frozen=True)
class _PatchHunk:
    index: int
    hint: str | None
    lines: tuple[_PatchLine, ...]


@dataclass(frozen=True)
class _FilePatch:
    path: str
    hunks: tuple[_PatchHunk, ...]


@dataclass(frozen=True)
class _ParsedPatch:
    files: tuple[_FilePatch, ...]


@dataclass(frozen=True)
class _LoadedFile:
    requested_path: str
    target: Path
    raw: bytes
    text: str
    normalized_text: str
    lines: tuple[str, ...]
    newline: str
    bom: bytes
    mode: int
    sha256: str
    signature: tuple[int, ...]
    final_newline: bool


@dataclass(frozen=True)
class _PreparedHunk:
    index: int
    start: int
    end: int
    start_line: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    added_lines: int
    removed_lines: int
    replacement: str


@dataclass(frozen=True)
class _PreparedFile:
    loaded: _LoadedFile
    hunks: tuple[_PreparedHunk, ...]
    encoded: bytes
    new_sha256: str
    lines_added: int
    lines_removed: int


class _PatchParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ApplyPatchTool
class ApplyPatchTool(FileTool):
    """Apply strict context-based patches to existing UTF-8 workspace files."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_patch_bytes: int = 512 * 1024,
        max_files: int = 32,
        max_hunks: int = 128,
        max_content_bytes: int = 2 * 1024 * 1024,
        max_added_lines: int = 10_000,
    ) -> None:
        super().__init__(
            workspace_root,
            max_patch_bytes=max_patch_bytes,
            max_files=max_files,
            max_hunks=max_hunks,
            max_content_bytes=max_content_bytes,
            max_added_lines=max_added_lines,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="apply_patch",
            description=(
                "Apply a structured line-based patch to one or more existing UTF-8 "
                "files inside the workspace. Use this for multi-line, multi-location, "
                "or multi-file changes. Patch context must match exactly and uniquely; "
                "fuzzy matching is never used. All files and hunks are validated before "
                "any target file is changed. For a small exact replacement, prefer "
                "edit_file. For replacing an entire file, prefer write_file. "
                "Only '*** Update File:' sections are supported; file creation, deletion, "
                "rename, mode changes, and binary patches are intentionally unsupported."
                " Files are rechecked before commit, but external writers are not locked "
                "and multi-file commits are not atomic. On commit failure inspect data.committed, "
                "data.not_committed and data.changes before retrying. Trailing text after @@ "
                "is a diagnostic label, not a location selector."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Patch in the form:\\n"
                            "*** Begin Patch\\n"
                            "*** Update File: path/to/file.py\\n"
                            "@@\\n"
                            " context line\\n"
                            "-old line\\n"
                            "+new line\\n"
                            " context line\\n"
                            "*** End Patch"
                        ),
                    },
                    "expected_files": {
                        "type": "array",
                        "maxItems": self.max_files,
                        "description": (
                            "Optional optimistic-concurrency checks. If a file was read "
                            "before constructing the patch, provide its SHA-256 here."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "minLength": 1,
                                },
                                "sha256": {
                                    "type": "string",
                                    "pattern": "^[0-9a-fA-F]{64}$",
                                },
                            },
                            "required": ["path", "sha256"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        )

    @_serialized_file_write
    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"patch", "expected_files"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: patch, expected_files.",
            )
        patch_text = arguments.get("patch")
        expected_files = arguments.get("expected_files")
        if not isinstance(patch_text, str) or not patch_text or "\x00" in patch_text:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "patch must be a non-empty string without NUL.",
            )
        try:
            patch_size = len(patch_text.encode("utf-8"))
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING, "patch must be valid UTF-8 text.")
        if patch_size > self.max_patch_bytes:
            return tool_error(
                "PATCH_TOO_LARGE", f"Patch exceeds {self.max_patch_bytes} UTF-8 bytes."
            )
        expected_or_error = self._parse_expected_files(expected_files)
        if isinstance(expected_or_error, ToolResult):
            return expected_or_error
        expected_files = expected_or_error

        try:
            parsed = self._parse_patch(patch_text)
        except _PatchParseError as exc:
            return tool_error(exc.code, exc.message)
        if len(parsed.files) > self.max_files:
            return tool_error(
                "TOO_MANY_PATCH_FILES",
                f"Patch contains {len(parsed.files)} files; limit is {self.max_files}.",
            )
        total_hunks = sum(len(file_patch.hunks) for file_patch in parsed.files)
        if total_hunks > self.max_hunks:
            return tool_error(
                "TOO_MANY_HUNKS",
                f"Patch contains {total_hunks} hunks; limit is {self.max_hunks}.",
            )
        patch_paths = {file_patch.path for file_patch in parsed.files}
        unused_expected = sorted(set(expected_files) - patch_paths)
        if unused_expected:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "expected_files contains paths not modified by the patch: "
                + ", ".join(unused_expected),
            )

        prepared_files: list[_PreparedFile] = []
        total_added_lines = 0
        seen_targets: set[Path] = set()
        seen_identities: set[tuple[int, int]] = set()

        # Phase 1: validate and prepare every result without changing any target.
        for file_patch in parsed.files:
            loaded_or_error = self._load_file(file_patch.path)
            if isinstance(loaded_or_error, ToolResult):
                return loaded_or_error
            loaded = loaded_or_error
            identity = loaded.signature[:2]
            if loaded.target in seen_targets or identity in seen_identities:
                return tool_error(
                    "DUPLICATE_FILE_SECTION",
                    f"Multiple patch paths refer to the same file: {file_patch.path}",
                )
            seen_targets.add(loaded.target)
            seen_identities.add(identity)

            expected_hash = expected_files.get(file_patch.path)
            if expected_hash is not None and loaded.sha256 != expected_hash:
                return tool_error(
                    ToolErrorCode.FILE_CHANGED, f"{file_patch.path} changed since it was read."
                )

            prepared_or_error = self._prepare_file(file_patch, loaded)
            if isinstance(prepared_or_error, ToolResult):
                return prepared_or_error
            prepared = prepared_or_error

            total_added_lines += prepared.lines_added
            if total_added_lines > self.max_added_lines:
                return tool_error(
                    "TOO_MANY_ADDED_LINES", f"Patch adds more than {self.max_added_lines} lines."
                )
            prepared_files.append(prepared)

        failure = self._commit_files(prepared_files)
        if failure is not None:
            return failure

        changes = [self._change_summary(prepared) for prepared in prepared_files]

        return ToolResult(
            success=True,
            data={
                "files_changed": len(prepared_files),
                "hunks_applied": total_hunks,
                "lines_added": sum(item.lines_added for item in prepared_files),
                "lines_removed": sum(item.lines_removed for item in prepared_files),
                "changes": changes,
            },
        )

    def _change_summary(self, prepared: _PreparedFile) -> dict[str, Any]:
        loaded = prepared.loaded
        return {
            "path": loaded.target.relative_to(self.workspace_root).as_posix(),
            "hunks": len(prepared.hunks),
            "lines_added": prepared.lines_added,
            "lines_removed": prepared.lines_removed,
            "bytes_before": len(loaded.raw),
            "bytes_after": len(prepared.encoded),
            "old_sha256": loaded.sha256,
            "new_sha256": prepared.new_sha256,
            "hunk_changes": [
                {
                    "hunk_index": hunk.index,
                    "start_line": hunk.start_line,
                    "old_line_count": len(hunk.old_lines),
                    "new_line_count": len(hunk.new_lines),
                }
                for hunk in prepared.hunks
            ],
        }

    def _commit_failure(self, prepared_files, committed, code, message, failed_path):
        count = len(committed)
        return ToolResult(
            False,
            data={
                "committed": [
                    item.loaded.target.relative_to(self.workspace_root).as_posix()
                    for item in committed
                ],
                "not_committed": [
                    item.loaded.target.relative_to(self.workspace_root).as_posix()
                    for item in prepared_files[count:]
                ],
                "changes": [self._change_summary(item) for item in committed],
                "files_changed": count,
                "failed_path": failed_path,
            },
            error_code=str(code),
            error=message,
        )

    def _check_unchanged(self, loaded: _LoadedFile) -> ToolResult | None:
        current = self._read_snapshot(loaded.requested_path)
        if (
            isinstance(current, ToolResult)
            or current.target != loaded.target
            or current.signature != loaded.signature
            or current.sha256 != loaded.sha256
        ):
            return tool_error(
                ToolErrorCode.FILE_CHANGED,
                f"{loaded.requested_path} changed or cannot be verified before commit; re-read it.",
            )
        return None

    def _commit_files(self, prepared_files: list[_PreparedFile]) -> ToolResult | None:
        # All exits, including cancellation during preparation, clean staging files.
        temp_paths: dict[Path, Path] = {}
        committed: list[_PreparedFile] = []
        phase = "prepare"
        failed_path = None
        with StagedWrites(temp_factory=NamedTemporaryFile, logger=logger) as staged:
            try:
                for prepared in prepared_files:
                    loaded = prepared.loaded
                    failed_path = loaded.requested_path
                    temp_paths[loaded.target] = staged.stage(
                        loaded.target, prepared.encoded, loaded.mode
                    )

                # Catch edits to ANY target before committing the first file.
                phase = "validate"
                for prepared in prepared_files:
                    failure = self._check_unchanged(prepared.loaded)
                    if failure is not None:
                        return self._commit_failure(
                            prepared_files,
                            committed,
                            failure.error_code,
                            failure.error,
                            prepared.loaded.requested_path,
                        )

                phase = "commit"
                for prepared in prepared_files:
                    loaded = prepared.loaded
                    failed_path = loaded.requested_path
                    # Check again immediately before each replacement. This narrows,
                    # but cannot eliminate, races with uncooperative external writers.
                    failure = self._check_unchanged(loaded)
                    if failure is not None:
                        return self._commit_failure(
                            prepared_files,
                            committed,
                            failure.error_code,
                            failure.error,
                            failed_path,
                        )
                    temp_paths[loaded.target].replace(loaded.target)
                    committed.append(prepared)
            except (OSError, RuntimeError) as error:
                code = (
                    "MULTI_FILE_COMMIT_FAILED"
                    if phase == "commit"
                    else (
                        ToolErrorCode.PERMISSION_DENIED
                        if isinstance(error, PermissionError)
                        else ToolErrorCode.WRITE_ERROR
                    )
                )
                return self._commit_failure(
                    prepared_files,
                    committed,
                    code,
                    f"Patch {phase} failed ({type(error).__name__}); "
                    "inspect committed files before retrying.",
                    failed_path,
                )
        return None

    def _parse_expected_files(
        self,
        value: Any,
    ) -> dict[str, str] | ToolResult:
        if value is None:
            return {}
        if not isinstance(value, list) or len(value) > self.max_files:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"expected_files must be an array of at most {self.max_files} items.",
            )

        result: dict[str, str] = {}
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS, f"expected_files[{index}] must be an object."
                )
            if set(item) != {"path", "sha256"}:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"expected_files[{index}] requires exactly path and sha256.",
                )
            path = item.get("path")
            sha256 = item.get("sha256")
            if not isinstance(path, str) or not path.strip() or "\x00" in path:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"expected_files[{index}].path must be a non-empty string without NUL.",
                )
            if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"expected_files[{index}].sha256 must be a "
                    "64-character hexadecimal SHA-256 digest.",
                )
            if path in result:
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"expected_files contains duplicate path: {path}",
                )
            result[path] = sha256.lower()

        return result

    def _parse_patch(self, patch_text: str) -> _ParsedPatch:
        text = patch_text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")

        # A final newline after "*** End Patch" is fine.
        if lines and lines[-1] == "":
            lines.pop()
        if not lines or lines[0] != "*** Begin Patch":
            raise _PatchParseError("INVALID_PATCH", "Patch must start with '*** Begin Patch'.")
        if lines[-1] != "*** End Patch":
            raise _PatchParseError("INVALID_PATCH", "Patch must end with '*** End Patch'.")

        file_patches: list[_FilePatch] = []
        seen_paths: set[str] = set()
        index = 1
        while index < len(lines) - 1:
            line = lines[index]
            if not line.startswith("*** Update File: "):
                raise _PatchParseError(
                    "INVALID_PATCH", f"Expected '*** Update File:' at patch line {index + 1}."
                )
            path = line[len("*** Update File: ") :]
            if not path.strip() or "\x00" in path:
                raise _PatchParseError(
                    "INVALID_PATCH_PATH", f"Invalid file path at patch line {index + 1}."
                )
            if path in seen_paths:
                raise _PatchParseError(
                    "DUPLICATE_FILE_SECTION", f"File appears more than once in patch: {path}"
                )
            seen_paths.add(path)
            index += 1

            hunks: list[_PatchHunk] = []
            while index < len(lines) - 1:
                line = lines[index]
                if line.startswith("*** Update File: "):
                    break
                if not line.startswith("@@"):
                    raise _PatchParseError(
                        "INVALID_PATCH", f"Expected hunk header '@@' at patch line {index + 1}."
                    )
                hint = line[2:].strip() or None
                hunk_index = len(hunks) + 1
                index += 1

                patch_lines: list[_PatchLine] = []
                saw_change = False
                while index < len(lines) - 1:
                    line = lines[index]
                    if line.startswith("@@") or line.startswith("*** Update File: "):
                        break
                    if not line:
                        raise _PatchParseError(
                            "INVALID_HUNK_LINE",
                            f"Patch line {index + 1} is empty. Blank content "
                            "lines must still carry a prefix: ' ', '+' or '-'.",
                        )
                    prefix = line[0]
                    content = line[1:]
                    if prefix == " ":
                        kind: Literal["context", "add", "remove"] = "context"
                    elif prefix == "+":
                        kind = "add"
                        saw_change = True
                    elif prefix == "-":
                        kind = "remove"
                        saw_change = True
                    else:
                        raise _PatchParseError(
                            "INVALID_HUNK_LINE",
                            f"Patch line {index + 1} must start with ' ', '+' or '-'.",
                        )
                    patch_lines.append(_PatchLine(kind=kind, text=content))
                    index += 1

                if not patch_lines:
                    raise _PatchParseError(
                        "EMPTY_HUNK",
                        f"Hunk {hunk_index} in {path} is empty.",
                    )
                if not saw_change:
                    raise _PatchParseError(
                        "NO_CHANGES",
                        f"Hunk {hunk_index} in {path} contains no additions or removals.",
                    )

                old_lines = [
                    item.text for item in patch_lines if item.kind in ("context", "remove")
                ]
                if not old_lines:
                    raise _PatchParseError(
                        "INSERTION_WITHOUT_CONTEXT",
                        f"Hunk {hunk_index} in {path} has no context/removal "
                        "lines. Pure insertion without an anchor is unsupported.",
                    )
                hunks.append(
                    _PatchHunk(
                        index=hunk_index,
                        hint=hint,
                        lines=tuple(patch_lines),
                    )
                )

            if not hunks:
                raise _PatchParseError("EMPTY_FILE_PATCH", f"{path} contains no hunks.")
            file_patches.append(_FilePatch(path=path, hunks=tuple(hunks)))

        if not file_patches:
            raise _PatchParseError(
                "EMPTY_PATCH",
                "Patch contains no file updates.",
            )

        return _ParsedPatch(files=tuple(file_patches))

    def _read_snapshot(self, path: str) -> FileSnapshot | ToolResult:
        try:
            candidate = self.workspace_root / path
            target = candidate.resolve()
            if is_credential_path(candidate, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            info = snapshot_stat(candidate)
            if S_ISLNK(info.st_mode):
                return tool_error(ToolErrorCode.PATH_IS_SYMLINK)
            if not S_ISREG(info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            return read_snapshot(
                candidate,
                target,
                info,
                self.max_content_bytes,
                verify_identity=True,
                size_message=f"{path} exceeds {self.max_content_bytes} bytes.",
            )
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND, f"File not found: {path}")
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (OSError, RuntimeError):
            return tool_error(ToolErrorCode.READ_ERROR)

    def _load_file(self, path: str) -> _LoadedFile | ToolResult:
        snapshot = self._read_snapshot(path)
        if isinstance(snapshot, ToolResult):
            return snapshot
        raw = snapshot.raw
        if b"\x00" in raw:
            return tool_error(ToolErrorCode.BINARY_FILE)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)
        newline_or_error = self._detect_newline(text, path)
        if isinstance(newline_or_error, ToolResult):
            return newline_or_error
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return _LoadedFile(
            requested_path=path,
            target=snapshot.target,
            raw=raw,
            text=text,
            normalized_text=normalized,
            lines=tuple(normalized.removesuffix("\n").split("\n")) if normalized else (),
            newline=newline_or_error,
            bom=b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"",
            mode=snapshot.info.st_mode & 0o777,
            sha256=snapshot.sha256,
            signature=snapshot.signature,
            final_newline=normalized.endswith("\n"),
        )

    def _detect_newline(
        self,
        text: str,
        path: str,
    ) -> str | ToolResult:
        crlf_count = text.count("\r\n")
        without_crlf = text.replace("\r\n", "")
        lf_count = without_crlf.count("\n")
        cr_count = without_crlf.count("\r")

        kinds = sum((crlf_count > 0, lf_count > 0, cr_count > 0))
        if kinds > 1:
            return tool_error(
                "MIXED_LINE_ENDINGS",
                f"{path} contains mixed line endings; patching is refused.",
            )
        if crlf_count:
            return "\r\n"
        if cr_count:
            return "\r"
        return "\n"

    def _prepare_file(
        self,
        file_patch: _FilePatch,
        loaded: _LoadedFile,
    ) -> _PreparedFile | ToolResult:
        source_lines = list(loaded.lines)
        prepared_hunks: list[_PreparedHunk] = []

        for hunk in file_patch.hunks:
            old_lines = tuple(
                item.text for item in hunk.lines if item.kind in ("context", "remove")
            )
            new_lines = tuple(item.text for item in hunk.lines if item.kind in ("context", "add"))

            matches = self._find_subsequence(source_lines, old_lines)
            if not matches:
                hint_suffix = f" ({hunk.hint})" if hunk.hint else ""
                return tool_error(
                    "CONTEXT_NOT_FOUND",
                    f"{file_patch.path}: hunk {hunk.index}{hint_suffix} "
                    "did not match the original file.",
                )
            if len(matches) > 1:
                candidate_lines = [item + 1 for item in matches[:20]]
                return tool_error(
                    "AMBIGUOUS_CONTEXT",
                    f"{file_patch.path}: hunk {hunk.index} matched more than "
                    f"once; candidate start lines: {candidate_lines}. "
                    "Add more surrounding context.",
                )

            start = matches[0]
            end = start + len(old_lines)
            # Preserve actual context-line terminators. In particular, deleting
            # an unterminated last line must not strip its predecessor's newline.
            fragments = []
            cursor = start
            for item in hunk.lines:
                if item.kind == "context":
                    terminated = cursor < len(source_lines) - 1 or loaded.final_newline
                    fragments.append(item.text + ("\n" if terminated else ""))
                    cursor += 1
                elif item.kind == "remove":
                    cursor += 1
                else:
                    fragments.append(item.text + "\n")
            last_output_kind = next(
                (item.kind for item in reversed(hunk.lines) if item.kind != "remove"), None
            )
            if (
                fragments
                and end == len(source_lines)
                and not loaded.final_newline
                and last_output_kind == "add"
            ):
                fragments[-1] = fragments[-1].removesuffix("\n")
            # Appending after an unterminated context line needs a separator.
            for i in range(len(fragments) - 1):
                if not fragments[i].endswith("\n"):
                    fragments[i] += "\n"
            prepared_hunks.append(
                _PreparedHunk(
                    index=hunk.index,
                    start=start,
                    end=end,
                    start_line=start + 1,
                    old_lines=old_lines,
                    new_lines=new_lines,
                    added_lines=sum(item.kind == "add" for item in hunk.lines),
                    removed_lines=sum(item.kind == "remove" for item in hunk.lines),
                    replacement="".join(fragments),
                )
            )

        ordered = sorted(prepared_hunks, key=lambda item: (item.start, item.end))
        for previous, current in pairwise(ordered):
            # v1 deliberately rejects overlap of the whole matched hunk,
            # including overlapping context, because this is simpler and safer.
            if current.start < previous.end:
                return tool_error(
                    "OVERLAPPING_HUNKS",
                    f"{file_patch.path}: hunk {previous.index} and "
                    f"hunk {current.index} overlap in the original file.",
                )

        updated_lines = [
            line + ("\n" if i < len(source_lines) - 1 or loaded.final_newline else "")
            for i, line in enumerate(source_lines)
        ]
        for hunk in sorted(prepared_hunks, key=lambda item: item.start, reverse=True):
            updated_lines[hunk.start : hunk.end] = [hunk.replacement]

        updated_normalized = "".join(updated_lines)
        updated_text = updated_normalized.replace("\n", loaded.newline)
        try:
            encoded = loaded.bom + updated_text.encode("utf-8")
        except UnicodeEncodeError:
            return tool_error(ToolErrorCode.UNSUPPORTED_ENCODING)

        if len(encoded) > self.max_content_bytes:
            return tool_error(
                "PATCH_RESULT_TOO_LARGE",
                f"{file_patch.path} would exceed {self.max_content_bytes} bytes after patching.",
            )

        if encoded == loaded.raw:
            return tool_error(
                "NO_CHANGES",
                f"{file_patch.path}: patch produces no byte-level change.",
            )

        return _PreparedFile(
            loaded=loaded,
            hunks=tuple(sorted(prepared_hunks, key=lambda item: item.start)),
            encoded=encoded,
            new_sha256=hashlib.sha256(encoded).hexdigest(),
            lines_added=sum(item.added_lines for item in prepared_hunks),
            lines_removed=sum(item.removed_lines for item in prepared_hunks),
        )

    @staticmethod
    def _find_subsequence(
        lines: list[str],
        pattern: tuple[str, ...],
    ) -> list[int]:
        if not pattern or len(pattern) > len(lines):
            return []

        # KMP: no candidate slices and O(n + m) line comparisons, including
        # repetitive input with a long near-match. Two matches prove ambiguity.
        prefix = [0] * len(pattern)
        matched = 0
        for i in range(1, len(pattern)):
            while matched and pattern[i] != pattern[matched]:
                matched = prefix[matched - 1]
            if pattern[i] == pattern[matched]:
                matched += 1
            prefix[i] = matched
        matches = []
        matched = 0
        for i, line in enumerate(lines):
            while matched and line != pattern[matched]:
                matched = prefix[matched - 1]
            if line == pattern[matched]:
                matched += 1
            if matched == len(pattern):
                matches.append(i - len(pattern) + 1)
                if len(matches) == 2:
                    break
                matched = prefix[matched - 1]
        return matches


# ListFileTool
class ListFileTool(FileTool):
    """List files in a directory with optional filtering and recursion."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_entries: int = 200,
    ) -> None:
        super().__init__(
            workspace_root,
            max_entries=max_entries,
        )

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
                        "description": (
                            "Path to a directory inside the workspace. "
                            "Use '.' for the workspace root."
                        ),
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to include entries whose names start with '.'.",
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
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        include_hidden = arguments.get("include_hidden", True)
        if not isinstance(include_hidden, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "include_hidden must be a boolean.")

        try:
            policy = self.path_policy()
            target = (self.workspace_root / path).resolve()
            if policy.is_protected(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
            entries: list[dict[str, Any]] = []
            access = current_file_access()
            candidates = (
                access.iterdir_entries(target) if access else ((p, None) for p in target.iterdir())
            )
            with closing(candidates):
                for entry, info in candidates:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    try:
                        if isinstance(info, OSError):
                            if policy.protects_path(entry, entry.resolve()):
                                continue
                            raise info
                        metadata = inspect_entry(entry, policy, info=info)
                        if metadata is None:
                            continue
                        entry_type = metadata.kind
                        size = metadata.info.st_size if entry_type == "file" else None
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
class FindFileTool(FileTool):
    """Find files or directories by glob pattern inside the workspace."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        max_results: int = 200,
    ) -> None:
        super().__init__(
            workspace_root,
            max_results=max_results,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_files",
            description=(
                "Find files or directories recursively inside the workspace using a glob pattern. "
                "Use this when you know a file or directory name/pattern "
                "but do not know its location. "
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
                ToolErrorCode.INVALID_ARGUMENTS,
                "pattern must be a non-empty string without NUL.",
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
                ToolErrorCode.INVALID_ARGUMENTS,
                "path must be a non-empty string without NUL.",
            )
        entry_type = arguments.get("type", "any")
        if not isinstance(entry_type, str) or entry_type not in {"file", "directory", "any"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "type must be one of: file, directory, any.",
            )
        include_hidden = arguments.get("include_hidden", True)
        if not isinstance(include_hidden, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "include_hidden must be a boolean.")

        try:
            policy = self.path_policy()
            abs_path = self.workspace_root / path
            target = abs_path.resolve()
            if policy.is_protected(abs_path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not target.is_dir():
                return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
            matches: list[dict[str, Any]] = []
            total_matches = 0
            access = current_file_access()
            candidates = (
                access.glob_entries(target, pattern)
                if access
                else ((p, None) for p in target.glob(pattern))
            )
            with closing(candidates):
                for candidate, info in candidates:
                    try:
                        metadata = inspect_entry(candidate, policy, info=info)
                        if metadata is None:
                            continue
                        if not metadata.resolved.is_relative_to(self.workspace_root):
                            continue
                        relative_to_search = candidate.relative_to(target)
                        if not include_hidden:
                            if any(part.startswith(".") for part in relative_to_search.parts):
                                continue
                        candidate_type = metadata.kind
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
                            item["size"] = metadata.info.st_size
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
class SearchFilesTool(FileTool):
    """Search for files in a directory with optional filtering and recursion."""

    execution_kind = ExecutionKind.TRUSTED_FILE

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
        super().__init__(
            workspace_root,
            max_results=max_results,
            max_file_bytes=max_file_bytes,
            max_line_chars=max_line_chars,
            max_files_scanned=max_files_scanned,
            max_output_chars=max_output_chars,
        )

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="search_files",
            description=(
                "Search UTF-8 text files inside the workspace for a literal text substring. "
                "Directories are searched recursively. Binary, oversized, unreadable, "
                "and unsupported-encoding files are skipped. "
                "Credential files and directories are always excluded, "
                "even with include_hidden=true. "
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
            policy = self.path_policy()
            requested = self.workspace_root / path
            target = requested.resolve()
            if policy.is_protected(requested, target):
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
            files = self._iter_files(
                target,
                include_hidden=include_hidden,
                glob=glob,
                policy=policy,
            )
            with closing(files):
                for candidate in files:
                    file, info = candidate.path, candidate.info
                    if files_scanned >= self.max_files_scanned:
                        truncated = True
                        truncation_reason = "max_files_scanned"
                        break
                    files_scanned += 1
                    try:
                        metadata = inspect_entry(file, policy, info=info)
                        if metadata is None or not metadata.resolved.is_relative_to(
                            self.workspace_root
                        ):
                            skipped_files += 1
                            continue
                        info = metadata.info
                        if not S_ISREG(info.st_mode) or info.st_size > self.max_file_bytes:
                            skipped_files += 1
                            continue
                        snapshot = read_snapshot(
                            file, file, info, self.max_file_bytes, directory=candidate.directory
                        )
                        if isinstance(snapshot, ToolResult):
                            skipped_files += 1
                            continue
                        raw = snapshot.raw
                        if b"\x00" in raw:
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
                    # Most repository files do not match. Avoid line allocation and
                    # Python iteration for that common literal-search case.
                    if case_sensitive and query not in text:
                        continue
                    file_matched = False
                    for line_number, line in enumerate(text_lines(text), start=1):
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
                        truncated_lines = self._truncate_matching_line(
                            line, match_index, len(query)
                        )
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

    def _iter_files(self, target: Path, *, include_hidden: bool, glob: str | None, policy):
        return iter_search_candidates(
            self.workspace_root,
            target,
            include_hidden=include_hidden,
            glob=glob,
            policy=policy,
        )

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
            return line[match_index : match_index + self.max_line_chars]
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
class MakeDirectoryTool(FileTool):
    """Create directories inside a bounded workspace."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(workspace_root)

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
                        "description": (
                            "Path to a directory inside the workspace. "
                            "Use '.' for the workspace root."
                        ),
                    },
                    "parents": {
                        "type": "boolean",
                        "default": False,
                        "description": "Whether to create missing parent directories.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    @_serialized_file_write
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
            self._mkdir(target, parents=parents, exist_ok=False)
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
class DeleteFileTool(FileTool):
    """Delete a file inside a bounded workspace."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(workspace_root)

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

    @_serialized_file_write
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
            access = current_file_access()
            access.unlink(target) if access else target.unlink()
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
class MoveFileTool(FileTool):
    """Move or rename one regular file inside a bounded workspace."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(workspace_root)

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
                        "description": ("Workspace-relative path of the existing file."),
                    },
                    "destination": {
                        "type": "string",
                        "minLength": 1,
                        "description": ("Workspace-relative destination path."),
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

    @_serialized_file_write
    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        """Validate model arguments and turn expected filesystem failures into tool errors."""
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"source", "destination", "create_parents"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: source, destination, create_parents",
            )
        source = arguments.get("source")
        if not isinstance(source, str) or not source.strip() or "\x00" in source:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "source must be a non-empty string without NUL."
            )
        destination = arguments.get("destination")
        if not isinstance(destination, str) or not destination.strip() or "\x00" in destination:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "destination must be a non-empty string without NUL.",
            )
        create_parents = arguments.get("create_parents", False)
        if not isinstance(create_parents, bool):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "create_parents must be a boolean.")

        try:
            source_candidate = self.workspace_root / source
            destination_candidate = self.workspace_root / destination
            source_target = source_candidate.resolve()
            destination_target = destination_candidate.resolve()
            if is_credential_path(source_candidate, source_target) or is_credential_path(
                destination_candidate, destination_target
            ):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not source_target.is_relative_to(self.workspace_root):
                return tool_error(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE, "source must stay inside the workspace."
                )
            if not destination_target.is_relative_to(self.workspace_root):
                return tool_error(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                    "denstination must stay inside the workspace.",
                )
            source_info = source_candidate.lstat()
            if S_ISLNK(source_info.st_mode):
                return tool_error(
                    ToolErrorCode.PATH_IS_SYMLINK, "Moving symbolic links is not supported."
                )
            if not S_ISREG(source_info.st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE, "source must refer to a regular file.")
            try:
                destination_candidate.lstat()
            except FileNotFoundError:
                pass
            else:
                return tool_error(
                    "DESTINATION_ALREADY_EXISTS", "The destination path already exists."
                )
            destination_parent = destination_candidate.parent.resolve()
            if not destination_parent.is_relative_to(self.workspace_root):
                return tool_error(
                    ToolErrorCode.PATH_OUTSIDE_WORKSPACE,
                    "The destination parent must stay inside the workspace.",
                )
            if not destination_parent.exists():
                if not create_parents:
                    return tool_error(
                        ToolErrorCode.PARENT_NOT_FOUND,
                        "The destination parent directory does not exist.",
                    )
                self._mkdir(destination_parent, parents=True, exist_ok=True)
            if not destination_parent.is_dir():
                return tool_error(
                    ToolErrorCode.NOT_A_DIRECTORY, "The destination parent must be a directory."
                )
            source_relative = source_target.relative_to(self.workspace_root).as_posix()
            destination_relative = destination_target.relative_to(self.workspace_root).as_posix()
            bytes_moved = source_info.st_size
            access = current_file_access()
            if access:
                access.rename(source_target, destination_target)
            else:
                source_candidate.rename(destination_candidate)
        except FileNotFoundError:
            return tool_error(ToolErrorCode.FILE_NOT_FOUND, "The source file does not exist.")
        except NotADirectoryError:
            return tool_error(ToolErrorCode.NOT_A_DIRECTORY)
        except PermissionError:
            return tool_error(
                ToolErrorCode.PERMISSION_DENIED,
                "The file cannot be moved with current permissions.",
            )
        except RuntimeError:
            return tool_error(
                "MOVE_ERROR",
                "Unable to move the file.",
            )
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return tool_error(
                    "CROSS_DEVICE_MOVE_NOT_SUPPORTED",
                    "Moving files across filesystems is not supported.",
                )
            return tool_error(
                "MOVE_ERROR",
                "Unable to move the file.",
            )

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
class GetPathInfoTool(FileTool):
    """Inspect filesystem metadata for one workspace path."""

    execution_kind = ExecutionKind.TRUSTED_FILE

    def __init__(
        self,
        workspace_root: str | Path,
    ) -> None:
        super().__init__(workspace_root)

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
                        "description": ("Path inside the workspace to inspect."),
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
                "The path cannot be inspected with current permissions.",
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
                data["executable"] = os.access(
                    candidate,
                    os.X_OK,
                )
            except OSError:
                data["executable"] = False
        return ToolResult(success=True, data=data)
