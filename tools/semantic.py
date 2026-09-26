"""Workspace code-analysis tools backed by a trusted language server.

Like command tools, these require an isolated execution context. Path checks do
not restrict the language server's own filesystem access. Each default call owns
and closes its client, matching the current single-request sandbox worker.
"""

import hashlib
import json
import logging
import os
import stat
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from llm import ToolDefinition

from ._internal.base import ExecutionKind, ToolResult
from ._internal.errors import ToolErrorCode, tool_error
from ._internal.file_policy import is_credential_path, is_protected_name
from ._internal.lsp_client import (
    LspClient,
    LspError,
    LspResponseError,
    LspTimeoutError,
    LspUnsupportedError,
)
from ._internal.lsp_config import LspLanguageConfig, LspRegistry, default_lsp_registry

logger = logging.getLogger(__name__)

_SYMBOL_KINDS = (
    "unknown",
    "file",
    "module",
    "namespace",
    "package",
    "class",
    "method",
    "property",
    "field",
    "constructor",
    "enum",
    "interface",
    "function",
    "variable",
    "constant",
    "string",
    "number",
    "boolean",
    "array",
    "object",
    "key",
    "null",
    "enum_member",
    "struct",
    "event",
    "operator",
    "type_parameter",
)


class LspToolBase:
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        legacy = any(value is not None for value in (command, language_id, file_extensions))
        if lsp_registry is not None and legacy:
            raise ValueError("Use either lsp_registry or single-language configuration")
        if legacy:
            python = default_lsp_registry().languages[0]
            config = LspLanguageConfig(
                "custom",
                language_id if language_id is not None else "python",
                file_extensions if file_extensions is not None else (".py", ".pyi"),
                command if command is not None else python.command,
            )
            lsp_registry = LspRegistry((config,))
        if lsp_registry is not None and not isinstance(lsp_registry, LspRegistry):
            raise ValueError("lsp_registry must be an LspRegistry")
        self.lsp_registry = lsp_registry if lsp_registry is not None else default_lsp_registry()
        self.timeout_seconds = timeout_seconds
        if timeout_seconds is not None:
            # Reuse the configuration's finite positive timeout validation.
            LspLanguageConfig("validation", "validation", (".tmp",), ("unused",), timeout_seconds)
        self.execution_allowed = execution_allowed

    def _validate_source_file(self, workspace_root: str | Path, path: str) -> Path | ToolResult:
        workspace_root = Path(workspace_root)
        target = (workspace_root / path).resolve()
        if is_credential_path(workspace_root / path, target):
            return tool_error(ToolErrorCode.PROTECTED_FILE)
        if not target.is_relative_to(workspace_root):
            return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
        if not stat.S_ISREG(target.stat().st_mode):
            return tool_error(ToolErrorCode.NOT_A_FILE)
        return target

    def _read_snapshot(
        self, lsp_registry: LspRegistry, workspace_root: str | Path, path: str
    ) -> tuple[Path, LspLanguageConfig, bytes, str] | ToolResult:
        try:
            target = self._validate_source_file(workspace_root, path)
            if isinstance(target, ToolResult):
                return target
            config = lsp_registry.select(target)
            if config is None:
                return tool_error(
                    "UNSUPPORTED_LANGUAGE", "No language server configured for this file."
                )
            if not self.execution_allowed:
                return tool_error(
                    ToolErrorCode.PERMISSION_DENIED,
                    "Language servers require an isolated execution context.",
                )
            with target.open("rb") as stream:
                raw = stream.read(self.max_file_bytes + 1)
            if len(raw) > self.max_file_bytes:
                return tool_error(ToolErrorCode.FILE_TOO_LARGE)
            if b"\0" in raw:
                return tool_error(ToolErrorCode.BINARY_FILE)
            text = raw.decode("utf-8")
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
        return target, config, raw, text

    def _verify_snapshot(self, target: Path, raw: bytes, max_file_bytes: int) -> ToolResult | None:
        with target.open("rb") as stream:
            if stream.read(max_file_bytes + 1) != raw:
                return tool_error(
                    ToolErrorCode.FILE_CHANGED, "File changed during analysis; retry."
                )

    def _new_client(self, config: LspLanguageConfig) -> LspClient:
        return LspClient(
            self.workspace_root,
            config.command,
            language_id=config.language_id,
            timeout_seconds=(
                self.timeout_seconds if self.timeout_seconds is not None else config.timeout_seconds
            ),
        )

    @contextmanager
    def _client_session(self, config: LspLanguageConfig):
        client = self._new_client(config)
        try:
            with client:
                yield client
        finally:
            # Keep diagnostics out of model output and the worker JSON protocol.
            if logger.isEnabledFor(logging.DEBUG):
                stderr = getattr(client, "stderr", "")
                if isinstance(stderr, str) and stderr:
                    logger.debug("LSP server %s stderr: %s", config.server_id, stderr[-8192:])

    @staticmethod
    def _source_position(text: str, line: int, column: int) -> tuple[int, int] | ToolResult:
        """Convert 1-based line/UTF-16 column to zero-based LSP position."""
        # LSP treats CRLF, LF and CR as line separators.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line > len(lines):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, f"line exceeds file length ({len(lines)} lines)"
            )

        source_line = lines[line - 1]
        utf16_length = len(source_line.encode("utf-16-le")) // 2
        # column=1 means LSP character=0.
        character = column - 1
        # End-of-line cursor position is valid.
        if character > utf16_length:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "column exceeds the end of the source line"
            )
        return line - 1, character

    @staticmethod
    def _range(value: Any) -> dict:
        if not isinstance(value, dict):
            raise ValueError("Invalid range")
        converted = {}
        positions = []
        for key in ("start", "end"):
            position = value[key]
            line, column = position["line"], position["character"]
            if any(type(x) is not int or x < 0 for x in (line, column)):
                raise ValueError("Invalid position")
            positions.append((line, column))
            converted[key] = {"line": line + 1, "column": column + 1}
        if positions[0] > positions[1]:
            raise ValueError("Reversed range")
        return converted

    @staticmethod
    def _file_uri_to_path(uri: str) -> Path | None:
        """Convert a local file:// URI into a platform Path."""
        if not isinstance(uri, str) or not uri:
            raise ValueError("Invalid definition URI")
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        # Remote file://host/path URIs are not local workspace paths.
        if parsed.netloc not in ("", "localhost"):
            if os.name != "nt":
                return None
            raw_path = f"//{parsed.netloc}{parsed.path}"
        else:
            raw_path = parsed.path

        try:
            decoded = url2pathname(raw_path)
            requested = Path(decoded)
            if "\0" in decoded or not requested.is_absolute() or parsed.query or parsed.fragment:
                raise ValueError("Expected an absolute local file URI")
            # Preserve aliases and '..' until the path policy has checked them.
            return requested
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("Invalid file URI") from error

    def _normalize_location(
        self, location: Any, index: int, allow_external_locations: bool
    ) -> tuple[dict[str, Any] | None, bool]:
        """Normalize Location or LocationLink.
        Returns:
            (normalized item or None, omitted_as_external)
        """
        if not isinstance(location, dict):
            raise ValueError("Invalid definition location")

        # --------------------------------------------------------------
        # LocationLink
        # {
        #   targetUri,
        #   targetRange,
        #   targetSelectionRange,
        #   originSelectionRange?
        # }
        # --------------------------------------------------------------
        if "targetUri" in location:
            uri = location["targetUri"]
            if not isinstance(uri, str):
                raise ValueError("Invalid target URI")
            item: dict[str, Any] = {
                "index": index,
                "range": self._range(location["targetRange"]),
                "selection_range": self._range(location["targetSelectionRange"]),
            }
            if location.get("originSelectionRange") is not None:
                item["origin_selection_range"] = self._range(location["originSelectionRange"])
        # --------------------------------------------------------------
        # Location
        # {
        #   uri,
        #   range
        # }
        # --------------------------------------------------------------
        elif "uri" in location:
            uri = location["uri"]
            if not isinstance(uri, str):
                raise ValueError("Invalid definition URI")
            item = {
                "index": index,
                "range": self._range(location["range"]),
            }
        else:
            raise ValueError("Expected Location or LocationLink")

        # --------------------------------------------------------------
        # URI / workspace policy
        # --------------------------------------------------------------
        requested = self._file_uri_to_path(uri)
        if requested is None:
            if not allow_external_locations:
                return None, True
            item["uri"] = uri
            item["in_workspace"] = False
            return item, False
        try:
            resolved = requested.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("Invalid file URI") from error
        if is_credential_path(requested, resolved):
            return None, True
        if resolved.is_relative_to(self.workspace_root):
            item["path"] = resolved.relative_to(self.workspace_root).as_posix()
            item["in_workspace"] = True
            return item, False
        if not allow_external_locations:
            return None, True
        item["uri"] = uri
        item["path"] = str(resolved)
        item["in_workspace"] = False
        return item, False

    def _normalize_locations(
        self,
        result: Any,
        *,
        start: int,
        limit: int,
        allow_external_locations: bool,
        sort_locations: bool = False,
    ) -> tuple[list[dict[str, Any]], int, int]:
        if result is None:
            raw_locations: list[Any] = []
        elif isinstance(result, dict):
            raw_locations = [result]
        elif isinstance(result, list):
            raw_locations = result
        else:
            raise ValueError("Expected Location, LocationLink, list or null")

        normalized_all: list[dict[str, Any]] = []
        omitted_external = 0
        seen: set[str] = set()

        for raw_location in raw_locations:
            item, omitted = self._normalize_location(
                raw_location, len(normalized_all), allow_external_locations
            )
            if omitted:
                omitted_external += 1
                continue
            if item is None:
                continue

            # Remove duplicate server results while preserving order.
            key = json.dumps(
                {k: v for k, v in item.items() if k != "index"},
                ensure_ascii=False,
                sort_keys=True,
            )
            if key in seen:
                continue
            seen.add(key)
            normalized_all.append(item)

        # References do not have a meaningful server ranking,
        # so deterministic sorting makes pagination stable.
        if sort_locations:

            def sort_key(item: dict[str, Any]) -> tuple:
                location = item.get("path", item.get("uri", ""))
                start_pos = item["range"]["start"]
                end_pos = item["range"]["end"]
                return (
                    not item.get("in_workspace", False),
                    location.casefold(),
                    location,
                    start_pos["line"],
                    start_pos["column"],
                    end_pos["line"],
                    end_pos["column"],
                )

            normalized_all.sort(key=sort_key)
        # Reassign index after filtering/deduplication.
        for index, item in enumerate(normalized_all):
            item["index"] = index
        total = len(normalized_all)
        page = normalized_all[start : start + limit]
        return page, total, omitted_external


# GetSymbolsTool
class GetSymbolsTool(LspToolBase):
    """List one document's symbols, with bounded, paginated tool output.

    Server configuration is constructor-only. Defaults route by source filename.
    A custom registry replaces the defaults; legacy command/language/extension
    arguments still construct a single-language registry.
    ``execution_allowed`` is a trusted caller switch, not an isolation mechanism.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_symbols: int = 200,
        max_output_chars: int = 51200,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_symbols", max_symbols),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_file_bytes = max_file_bytes
        self.max_symbols = max_symbols
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_symbols",
            description=(
                "List classes, functions, methods and other symbols in one workspace file "
                f"using a language server. Configured support: {self.lsp_registry.description}. "
                "Results use 1-based lines and UTF-16 columns; range ends are exclusive. "
                "start_index is a zero-based index into the flattened symbol list. "
                "Use next_start_index to continue; do not edit the file between pages. "
                "Parent indices are supplied only for hierarchical server results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path to a source file inside the workspace.",
                    },
                    "start_index": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_symbols,
                        "default": self.max_symbols,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "start_index", "limit"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, start_index, limit.",
            )
        path = arguments.get("path")
        start = arguments.get("start_index", 0)
        limit = arguments.get("limit", self.max_symbols)
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL."
            )
        if type(start) is not int or start < 0:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "start must be a non-negative integer."
            )
        if type(limit) is not int or not 1 <= limit <= self.max_symbols:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "limit must be a integer between 1 and max_symbols.",
            )

        _read_snapshot_result = self._read_snapshot(self.lsp_registry, self.workspace_root, path)
        if isinstance(_read_snapshot_result, ToolResult):
            return _read_snapshot_result
        target, config, raw, text = _read_snapshot_result

        # ------------------------------------------------------------
        # Ask the language server.
        # ------------------------------------------------------------
        try:
            with self._client_session(config) as client:
                capability = client.capabilities.get("documentSymbolProvider")
                if capability is not True and not isinstance(capability, dict):
                    raise LspUnsupportedError("Server does not support document symbols")
                # Send the validated snapshot without re-reading the file.
                uri = client.sync_document(target, text=text)
                symbols = client.request(
                    "textDocument/documentSymbol", {"textDocument": {"uri": uri}}
                )
            page, total, hierarchy = self._normalize_symbols(symbols, target.as_uri(), start, limit)
            # Verify the source file did not change during analysis.
            verification_error = self._verify_snapshot(target, raw, self.max_file_bytes)
            if verification_error is not None:
                return verification_error
        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED", "Server does not support document symbols or sync."
            )
        except LspResponseError:
            return tool_error("LSP_REQUEST_FAILED", "Language server rejected the symbols request.")
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )
        except (ValueError, TypeError, KeyError):
            return tool_error("LSP_INVALID_RESPONSE", "Server returned invalid document symbols.")

        if start > total:
            return tool_error("INDEX_OUT_OF_RANGE", f"start_index exceeds {total} symbols.")
        next_index = start + len(page)
        data = {
            "path": target.relative_to(self.workspace_root).as_posix(),
            "language_id": config.language_id,
            "server_id": config.server_id,
            "symbols": page,
            "total_symbols": total,
            "hierarchical": hierarchy,
            "start_index": start,
            "truncated": next_index < total,
            "next_start_index": next_index if next_index < total else None,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if len(json.dumps(data, ensure_ascii=False)) > self.max_output_chars:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE, "Symbol output is too large; reduce limit."
            )
        return ToolResult(success=True, data=data)

    @classmethod
    def _normalize_symbols(
        cls, symbols: Any, uri: str, start: int, limit: int
    ) -> tuple[list, int, bool]:
        if symbols is None:
            return [], 0, False
        if not isinstance(symbols, list):
            raise ValueError("Expected symbol list")
        hierarchy = bool(symbols and isinstance(symbols[0], dict) and "location" not in symbols[0])
        stack = [(iter(symbols), None)]
        exhausted = object()
        page = []
        total = 0
        # Iterative traversal supports deeply nested classes without Python recursion.
        while stack:
            iterator, parent_index = stack[-1]
            symbol = next(iterator, exhausted)
            if symbol is exhausted:
                stack.pop()
                continue
            if not isinstance(symbol, dict) or ("location" not in symbol) != hierarchy:
                raise ValueError("Invalid or mixed symbol formats")
            name, kind = symbol["name"], symbol["kind"]
            if not isinstance(name, str) or not name.strip() or type(kind) is not int:
                raise ValueError("Invalid symbol name or kind")
            location = symbol if hierarchy else symbol["location"]
            if not isinstance(location, dict) or (not hierarchy and location.get("uri") != uri):
                raise ValueError("Document symbol refers to another file")
            item = {
                "index": total,
                "name": name,
                "kind": _SYMBOL_KINDS[kind] if 1 <= kind < len(_SYMBOL_KINDS) else "unknown",
                "range": cls._range(location["range"]),
            }
            if hierarchy:
                item["selection_range"] = cls._range(symbol["selectionRange"])
                item["parent_index"] = parent_index
                children = symbol.get("children", [])
                if not isinstance(children, list):
                    raise ValueError("Invalid children")
                stack.append((iter(children), total))
            elif symbol.get("containerName") is not None:
                if not isinstance(symbol["containerName"], str):
                    raise ValueError("Invalid container name")
                item["container_name"] = symbol["containerName"]
            if symbol.get("detail") is not None:
                if not isinstance(symbol["detail"], str):
                    raise ValueError("Invalid symbol detail")
                item["detail"] = symbol["detail"]
            if start <= total < start + limit:
                page.append(item)
            total += 1
        return page, total, hierarchy


# GoToDefinitionsTool
class GoToDefinitionsTool(LspToolBase):
    """Resolve the definition of a symbol at a source position using LSP.

    Server configuration is constructor-only. Defaults route by source filename.
    A custom registry replaces the defaults; legacy command/language/extension
    arguments still construct a single-language registry.
    ``execution_allowed`` is a trusted caller switch, not an isolation mechanism.

    Model-facing positions use 1-based lines and 1-based UTF-16 columns.
    LSP positions are converted internally to zero-based coordinates.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_definitions: int = 200,
        max_output_chars: int = 51200,
        allow_external_locations: bool = False,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        if type(allow_external_locations) is not bool:
            raise ValueError("allow_external_locations must be a boolean")
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_definitions", max_definitions),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.allow_external_locations = allow_external_locations
        self.max_file_bytes = max_file_bytes
        self.max_definitions = max_definitions
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="go_to_definition",
            description=(
                "Find the definition of the symbol at a position in a workspace source file "
                f"using a language server. Configured support: {self.lsp_registry.description}. "
                "Input and output positions use 1-based lines and 1-based UTF-16 columns; "
                "range ends are exclusive. "
                "Use a position inside the symbol name. "
                "start_index is a zero-based index into the returned definition list. "
                "Use next_start_index to continue if results are truncated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": ("Path to a source file inside the workspace."),
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": ("1-based source line containing the symbol."),
                    },
                    "column": {
                        "type": "integer",
                        "minimum": 1,
                        "description": ("1-based UTF-16 column inside the symbol name."),
                    },
                    "start_index": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_definitions,
                        "default": self.max_definitions,
                    },
                },
                "required": ["path", "line", "column"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "line", "column", "start_index", "limit"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, line, column, start_index, limit.",
            )
        path = arguments.get("path")
        line = arguments.get("line")
        column = arguments.get("column")
        start = arguments.get("start_index", 0)
        limit = arguments.get("limit", self.max_definitions)
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL."
            )
        if type(line) is not int or line < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "line must be a positive integer.")
        if type(column) is not int or column < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "column must be a positive integer.")
        if type(start) is not int or start < 0:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "start must be a non-negative integer."
            )
        if type(limit) is not int or not 1 <= limit <= self.max_definitions:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "limit must be a integer between 1 and max_definitions.",
            )

        _read_snapshot_result = self._read_snapshot(self.lsp_registry, self.workspace_root, path)
        if isinstance(_read_snapshot_result, ToolResult):
            return _read_snapshot_result
        target, config, raw, text = _read_snapshot_result
        # Convert model-facing 1-based coordinates into LSP coordinates.
        _source_position_result = self._source_position(text, line, column)
        if isinstance(_source_position_result, ToolResult):
            return _source_position_result
        lsp_line, lsp_character = _source_position_result

        # ------------------------------------------------------------
        # Ask the language server.
        # ------------------------------------------------------------
        try:
            with self._client_session(config) as client:
                capability = client.capabilities.get("definitionProvider")
                if capability is not True and not isinstance(capability, dict):
                    raise LspUnsupportedError("Server does not support definitions")
                # Send the validated snapshot without re-reading the file.
                uri = client.sync_document(target, text=text)
                result = client.request(
                    "textDocument/definition",
                    {
                        "textDocument": {"uri": uri},
                        "position": {
                            "line": lsp_line,
                            "character": lsp_character,
                        },
                    },
                )
            definitions, total, omitted_external = self._normalize_locations(
                result,
                start=start,
                limit=limit,
                allow_external_locations=self.allow_external_locations,
            )
            # Verify the source file did not change during analysis.
            verification_error = self._verify_snapshot(target, raw, self.max_file_bytes)
            if verification_error is not None:
                return verification_error

        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED", "Server does not support definitions or document sync."
            )
        except LspResponseError:
            return tool_error(
                "LSP_REQUEST_FAILED", "Language server rejected the definition request."
            )
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (ValueError, TypeError, KeyError):
            return tool_error(
                "LSP_INVALID_RESPONSE", "Server returned invalid definition locations."
            )
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )

        if start > total:
            return tool_error("INDEX_OUT_OF_RANGE", f"start_index exceeds {total} definitions.")
        next_index = start + len(definitions)
        data = {
            "path": target.relative_to(self.workspace_root).as_posix(),
            "position": {
                "line": line,
                "column": column,
            },
            "language_id": config.language_id,
            "server_id": config.server_id,
            "definitions": definitions,
            "total_definitions": total,
            "start_index": start,
            "truncated": next_index < total,
            "next_start_index": next_index if next_index < total else None,
            "omitted_external_definitions": omitted_external,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if len(json.dumps(data, ensure_ascii=False)) > self.max_output_chars:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE, "Definition output is too large; reduce limit."
            )

        return ToolResult(
            success=True,
            data=data,
        )


# FindReferencesTool
class FindReferencesTool(LspToolBase):
    """Find references to the symbol at a source position using LSP.

    Server configuration is constructor-only. Defaults route by source filename.
    A custom registry replaces the defaults; legacy command/language/extension
    arguments still construct a single-language registry.
    ``execution_allowed`` is a trusted caller switch, not an isolation mechanism.

    Model-facing positions use 1-based lines and 1-based UTF-16 columns.
    LSP positions are converted internally to zero-based coordinates.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_references: int = 200,
        max_output_chars: int = 51200,
        allow_external_locations: bool = False,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        if type(allow_external_locations) is not bool:
            raise ValueError("allow_external_locations must be a boolean")
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_references", max_references),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.allow_external_locations = allow_external_locations
        self.max_file_bytes = max_file_bytes
        self.max_references = max_references
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="find_references",
            description=(
                "Find references to the symbol at a position in a workspace source file "
                f"using a language server. Configured support: {self.lsp_registry.description}. "
                "Input and output positions use 1-based lines and 1-based UTF-16 columns; "
                "range ends are exclusive. "
                "Use a position inside the symbol name. "
                "Set include_declaration to include the declaration/definition when supported. "
                "start_index is a zero-based index into the normalized reference list. "
                "Use next_start_index to continue if results are truncated; "
                "do not edit related workspace files between pages. "
                "sha256 identifies only the queried source file, not the whole reference set."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": ("Path to a source file inside the workspace."),
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": ("1-based source line containing the symbol."),
                    },
                    "column": {
                        "type": "integer",
                        "minimum": 1,
                        "description": ("1-based UTF-16 column inside the symbol name."),
                    },
                    "include_declaration": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Whether the language server should include the symbol's "
                            "declaration in the returned references."
                        ),
                    },
                    "start_index": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_references,
                        "default": self.max_references,
                    },
                },
                "required": [
                    "path",
                    "line",
                    "column",
                ],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {
            "path", "line", "column", "include_declaration", "start_index", "limit",
        }:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, line, column, include_declaration, start_index, limit.",
            )
        path = arguments.get("path")
        line = arguments.get("line")
        column = arguments.get("column")
        include_declaration = arguments.get("include_declaration", True)
        start = arguments.get("start_index", 0)
        limit = arguments.get("limit", self.max_references)
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL."
            )
        if type(line) is not int or line < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "line must be a positive integer.")
        if type(column) is not int or column < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "column must be a positive integer.")
        if type(include_declaration) is not bool:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "include_declaration must be a boolean."
            )
        if type(start) is not int or start < 0:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "start must be a non-negative integer."
            )
        if type(limit) is not int or not 1 <= limit <= self.max_references:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "limit must be a integer between 1 and max_references.",
            )

        _read_snapshot_result = self._read_snapshot(self.lsp_registry, self.workspace_root, path)
        if isinstance(_read_snapshot_result, ToolResult):
            return _read_snapshot_result
        target, config, raw, text = _read_snapshot_result
        # Convert model-facing 1-based coordinates into LSP coordinates.
        _source_position_result = self._source_position(text, line, column)
        if isinstance(_source_position_result, ToolResult):
            return _source_position_result
        lsp_line, lsp_character = _source_position_result

        # ------------------------------------------------------------
        # Ask the language server.
        # ------------------------------------------------------------
        try:
            with self._client_session(config) as client:
                capability = client.capabilities.get("referencesProvider")
                if capability is not True and not isinstance(capability, dict):
                    raise LspUnsupportedError("Server does not support references")
                # Send the validated snapshot without re-reading the file.
                uri = client.sync_document(target, text=text)
                result = client.request(
                    "textDocument/references",
                    {
                        "textDocument": {"uri": uri},
                        "position": {
                            "line": lsp_line,
                            "character": lsp_character,
                        },
                        "context": {
                            "includeDeclaration": include_declaration,
                        },
                    },
                )
            if result is not None and (
                not isinstance(result, list)
                or any(
                    not isinstance(location, dict)
                    or "uri" not in location
                    or "range" not in location
                    or "targetUri" in location
                    for location in result
                )
            ):
                raise ValueError("References must be a Location array or null")
            references, total, omitted_external = self._normalize_locations(
                result,
                start=start,
                limit=limit,
                allow_external_locations=self.allow_external_locations,
                sort_locations=True,
            )
            # Verify the source file did not change during analysis.
            verification_error = self._verify_snapshot(target, raw, self.max_file_bytes)
            if verification_error is not None:
                return verification_error

        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED", "Server does not support references or document sync."
            )
        except LspResponseError:
            return tool_error(
                "LSP_REQUEST_FAILED", "Language server rejected the references request."
            )
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (ValueError, TypeError, KeyError):
            return tool_error(
                "LSP_INVALID_RESPONSE", "Server returned invalid references locations."
            )
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )

        if start > total:
            return tool_error("INDEX_OUT_OF_RANGE", f"start_index exceeds {total} references.")
        next_index = start + len(references)
        data = {
            "path": target.relative_to(self.workspace_root).as_posix(),
            "position": {
                "line": line,
                "column": column,
            },
            "language_id": config.language_id,
            "server_id": config.server_id,
            "include_declaration": include_declaration,
            "references": references,
            "total_references": total,
            "start_index": start,
            "truncated": next_index < total,
            "next_start_index": next_index if next_index < total else None,
            "omitted_external_references": omitted_external,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if len(json.dumps(data, ensure_ascii=False)) > self.max_output_chars:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE, "Reference output is too large; reduce limit."
            )

        return ToolResult(
            success=True,
            data=data,
        )


_DIAGNOSTIC_SEVERITIES = {
    1: "error",
    2: "warning",
    3: "information",
    4: "hint",
}

_DIAGNOSTIC_TAGS = {
    1: "unnecessary",
    2: "deprecated",
}


# GetDiagnosticsTool
class GetDiagnosticsTool(LspToolBase):
    """Get language-server diagnostics for one workspace source file.

    Supports both pull diagnostics and publishDiagnostics-based servers
    through LspClient.

    Server configuration is constructor-only. Defaults route by source filename.
    A custom registry replaces the defaults; legacy command/language/extension
    arguments still construct a single-language registry.
    ``execution_allowed`` is a trusted caller switch, not an isolation mechanism.

    Model-facing positions use 1-based lines and 1-based UTF-16 columns.
    LSP positions are converted internally to zero-based coordinates.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_diagnostics: int = 200,
        max_output_chars: int = 51200,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_diagnostics", max_diagnostics),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_file_bytes = max_file_bytes
        self.max_diagnostics = max_diagnostics
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_diagnostics",
            description=(
                "Get syntax, type, semantic and other diagnostics reported by "
                "the configured language server for one workspace source file. "
                f"Configured support: {self.lsp_registry.description}. "
                "Positions use 1-based lines and 1-based UTF-16 columns; "
                "range ends are exclusive. "
                "total_diagnostics=0 means the server reported no diagnostics; "
                "an empty page alone does not. Unversioned push reports have "
                "freshness_verified=false and may be stale. Even verified reports "
                "do not prove analysis is complete or cross-file dependencies are current. "
                "start_index is a zero-based index into the normalized diagnostic list. "
                "Use next_start_index to continue if results are truncated. "
                "Do not edit related workspace files between pages."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": ("Path to a source file inside the workspace."),
                    },
                    "start_index": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_diagnostics,
                        "default": self.max_diagnostics,
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "start_index", "limit"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, start_index, limit.",
            )
        path = arguments.get("path")
        start = arguments.get("start_index", 0)
        limit = arguments.get("limit", self.max_diagnostics)
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL."
            )
        if type(start) is not int or start < 0:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "start must be a non-negative integer."
            )
        if type(limit) is not int or not 1 <= limit <= self.max_diagnostics:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "limit must be a integer between 1 and max_diagnostics.",
            )

        _read_snapshot_result = self._read_snapshot(self.lsp_registry, self.workspace_root, path)
        if isinstance(_read_snapshot_result, ToolResult):
            return _read_snapshot_result
        target, config, raw, text = _read_snapshot_result

        # ------------------------------------------------------------
        # Ask the language server.
        # ------------------------------------------------------------
        try:
            with self._client_session(config) as client:
                report = client.get_diagnostics(target, text=text)
            if not isinstance(report, dict):
                raise ValueError("Invalid diagnostic report")
            if report.get("uri") != target.as_uri():
                raise ValueError("Diagnostic report refers to another document")
            report_source = report.get("source")
            if report_source not in {
                "pull",
                "push",
            }:
                raise ValueError("Invalid diagnostic source")
            version = report.get("version")
            if version is not None and (type(version) is not int or version < 0):
                raise ValueError("Invalid diagnostic version")
            items = report.get("items")
            diagnostics, total, severity_counts = self._normalize_diagnostics(
                items, start=start, limit=limit
            )

            # Verify the source file did not change during analysis.
            verification_error = self._verify_snapshot(target, raw, self.max_file_bytes)
            if verification_error is not None:
                return verification_error

        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED", "Server does not support diagnostics or document sync."
            )
        except LspResponseError:
            return tool_error(
                "LSP_REQUEST_FAILED", "Language server rejected the diagnostics request."
            )
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (ValueError, TypeError, KeyError):
            return tool_error(
                "LSP_INVALID_RESPONSE", "Server returned invalid diagnostics locations."
            )
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )

        if start > total:
            return tool_error("INDEX_OUT_OF_RANGE", f"start_index exceeds {total} diagnostics.")
        next_index = start + len(diagnostics)
        freshness_verified = report_source == "pull" or version is not None
        data = {
            "path": target.relative_to(self.workspace_root).as_posix(),
            "language_id": config.language_id,
            "server_id": config.server_id,
            "diagnostic_source": report_source,
            "diagnostic_version": version,
            "freshness_verified": freshness_verified,
            "diagnostics": diagnostics,
            "total_diagnostics": total,
            "severity_counts": severity_counts,
            "start_index": start,
            "truncated": next_index < total,
            "next_start_index": next_index if next_index < total else None,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if len(json.dumps(data, ensure_ascii=False)) > self.max_output_chars:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE, "Diagnostic output is too large; reduce limit."
            )

        return ToolResult(
            success=True,
            data=data,
        )

    @classmethod
    def _normalize_diagnostics(
        cls,
        items: Any,
        *,
        start: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        if not isinstance(items, list):
            raise ValueError("Expected diagnostic list")

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()

        for diagnostic in items:
            if not isinstance(diagnostic, dict):
                raise ValueError("Invalid diagnostic")
            message = diagnostic.get("message")
            if not isinstance(message, str):
                raise ValueError("Invalid diagnostic message")

            # Severity
            severity_value = diagnostic.get("severity")
            if severity_value is None:
                severity = "unspecified"
            elif type(severity_value) is int and severity_value in _DIAGNOSTIC_SEVERITIES:
                severity = _DIAGNOSTIC_SEVERITIES[severity_value]
            else:
                raise ValueError("Invalid diagnostic severity")

            item: dict[str, Any] = {
                "range": cls._range(diagnostic["range"]),
                "severity": severity,
                "message": message,
            }

            # Optional source
            source = diagnostic.get("source")
            if source is not None:
                if not isinstance(source, str):
                    raise ValueError("Invalid diagnostic source")
                item["source"] = source

            # Optional code
            code = diagnostic.get("code")
            if code is not None:
                if not (isinstance(code, str) or type(code) is int):
                    raise ValueError("Invalid diagnostic code")
                item["code"] = code

            # Optional tags
            tags = diagnostic.get("tags")
            if tags is not None:
                if not isinstance(tags, list):
                    raise ValueError("Invalid diagnostic tags")
                normalized_tags: list[str] = []
                for tag in tags:
                    if type(tag) is not int or tag not in _DIAGNOSTIC_TAGS:
                        raise ValueError("Invalid diagnostic tag")
                    normalized_tags.append(_DIAGNOSTIC_TAGS[tag])
                if normalized_tags:
                    item["tags"] = normalized_tags

            # -----------------------------
            # Related information
            #
            # Keep only the count in the
            # first version. Full related
            # locations need their own
            # workspace/external policy.
            # -----------------------------
            related = diagnostic.get("relatedInformation")
            if related is not None:
                if not isinstance(related, list):
                    raise ValueError("Invalid related information")
                if related:
                    item["related_information_count"] = len(related)
            # Do not expose Diagnostic.data.
            # It is server-specific and can
            # be arbitrarily large.
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(item)

        # ------------------------------------------------------------
        # Stable ordering.
        #
        # Diagnostic ordering is not semantically meaningful, while
        # pagination across fresh LSP sessions benefits from stable
        # deterministic ordering.
        # ------------------------------------------------------------
        severity_order = {
            "error": 0,
            "warning": 1,
            "information": 2,
            "hint": 3,
            "unspecified": 4,
        }
        normalized.sort(
            key=lambda item: (
                item["range"]["start"]["line"],
                item["range"]["start"]["column"],
                item["range"]["end"]["line"],
                item["range"]["end"]["column"],
                severity_order[item["severity"]],
                str(item.get("source", "")).casefold(),
                str(item.get("code", "")),
                item["message"],
                json.dumps(item, ensure_ascii=False, sort_keys=True),
            )
        )

        # Indices are assigned only after
        # deduplication and sorting.
        for index, item in enumerate(normalized):
            item["index"] = index

        severity_counts = {
            "error": 0,
            "warning": 0,
            "information": 0,
            "hint": 0,
            "unspecified": 0,
        }
        for item in normalized:
            severity_counts[item["severity"]] += 1

        total = len(normalized)
        page = normalized[start : start + limit]
        return page, total, severity_counts


# GetHoverTool
class GetHoverTool(LspToolBase):
    """Get type, signature and documentation information for a source position.

    Server configuration is constructor-only. Defaults route by source filename.
    A custom registry replaces the defaults; legacy command/language/extension
    arguments still construct a single-language registry.

    Model-facing positions use 1-based lines and 1-based UTF-16 columns.
    LSP positions are converted internally to zero-based coordinates.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_file_bytes: int = 2 * 1024 * 1024,
        max_hover_chars: int = 12_000,
        max_output_chars: int = 51200,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_hover_chars", max_hover_chars),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_file_bytes = max_file_bytes
        self.max_hover_chars = max_hover_chars
        self.max_output_chars = max_output_chars

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_hover",
            description=(
                "Get type, signature, documentation and other hover information "
                "for the symbol at a position in a workspace source file using "
                "a language server. "
                f"Configured support: {self.lsp_registry.description}. "
                "Input and output positions use 1-based lines and 1-based UTF-16 "
                "columns; range ends are exclusive. "
                "Use a position inside the symbol name. "
                "A null hover result means the language server has no hover "
                "information for that position."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Path to a source file inside the workspace."
                        ),
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "1-based source line containing the symbol."
                        ),
                    },
                    "column": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "1-based UTF-16 column inside the symbol name."
                        ),
                    },
                },
                "required": [
                    "path",
                    "line",
                    "column",
                ],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"path", "line", "column"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: path, line, column.",
            )
        path = arguments.get("path")
        line = arguments.get("line")
        column = arguments.get("column")
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "path must be a non-empty string without NUL."
            )
        if type(line) is not int or line < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "line must be a positive integer.")
        if type(column) is not int or column < 1:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS, "column must be a positive integer.")

        _read_snapshot_result = self._read_snapshot(self.lsp_registry, self.workspace_root, path)
        if isinstance(_read_snapshot_result, ToolResult):
            return _read_snapshot_result
        target, config, raw, text = _read_snapshot_result
        # Convert model-facing 1-based coordinates into LSP coordinates.
        _source_position_result = self._source_position(text, line, column)
        if isinstance(_source_position_result, ToolResult):
            return _source_position_result
        lsp_line, lsp_character = _source_position_result

        # ------------------------------------------------------------
        # Ask the language server.
        # ------------------------------------------------------------
        try:
            with self._client_session(config) as client:
                capability = client.capabilities.get("hoverProvider")
                if capability is not True and not isinstance(capability, dict):
                    raise LspUnsupportedError("Server does not support hover")
                # Send the validated snapshot without re-reading the file.
                uri = client.sync_document(target, text=text)
                result = client.request(
                        "textDocument/hover",
                        {
                            "textDocument": {"uri": uri,},
                            "position": {
                                "line": lsp_line,
                                "character": lsp_character,
                            },
                        },
                    )
            hover = self._normalize_hover(result, max_chars=self.max_hover_chars)

            # Verify the source file did not change during analysis.
            verification_error = self._verify_snapshot(target, raw, self.max_file_bytes)
            if verification_error is not None:
                return verification_error

        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED", "Server does not support hover or document sync."
            )
        except LspResponseError:
            return tool_error(
                "LSP_REQUEST_FAILED", "Language server rejected the hover request."
            )
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (ValueError, TypeError, KeyError):
            return tool_error(
                "LSP_INVALID_RESPONSE", "Server returned invalid hover response."
            )
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )

        # ------------------------------------------------------------
        # Build Agent-facing response.
        # ------------------------------------------------------------
        data = {
            "path": target.relative_to(self.workspace_root).as_posix(),
            "position": {
                "line": line,
                "column": column,
            },
            "language_id": config.language_id,
            "server_id": config.server_id,
            "hover": hover,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        data = self._fit_hover_output(data)
        if data is None:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE,
                "Hover metadata exceeds the configured output limit.",
            )

        return ToolResult(success=True, data=data)

    def _fit_hover_output(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Keep a content prefix that fits the complete serialized response."""

        def fits(candidate: dict[str, Any]) -> bool:
            return len(json.dumps(candidate, ensure_ascii=False)) <= self.max_output_chars

        if fits(data):
            return data
        hover = data["hover"]
        if hover is None:
            return None
        parts = hover["contents"]
        char_counts = [0]
        for part in parts:
            char_counts.append(char_counts[-1] + len(part["value"]))

        def prefix(count: int, clipped: dict[str, Any] | None = None) -> dict[str, Any]:
            contents = parts[:count]
            returned_chars = char_counts[count]
            if clipped is not None:
                contents.append(clipped)
                returned_chars += len(clipped["value"])
            return {
                **data,
                "hover": {
                    **hover,
                    "contents": contents,
                    "returned_content_chars": returned_chars,
                    "content_truncated": hover["content_truncated"] or count < len(parts),
                },
            }

        best = prefix(0)
        if not fits(best):
            return None

        # Search by part count as well as text length: empty/short parts and
        # language metadata can exhaust the output budget without much text.
        low, high = 0, len(parts)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = prefix(middle)
            if fits(candidate):
                low, best = middle, candidate
            else:
                high = middle - 1

        if low < len(parts):
            count = low
            part = parts[count]
            low, high = 1, len(part["value"]) - 1
            while low <= high:
                middle = (low + high) // 2
                clipped = {**part, "value": part["value"][:middle], "truncated": True}
                candidate = prefix(count, clipped)
                if fits(candidate):
                    best = candidate
                    low = middle + 1
                else:
                    high = middle - 1
        return best

    @classmethod
    def _normalize_hover(
        cls,
        hover: Any,
        *,
        max_chars: int,
    ) -> dict[str, Any] | None:
        # No hover information at this position is a valid result.
        if hover is None:
            return None
        if not isinstance(hover, dict):
            raise ValueError("Expected Hover object or null")
        if "contents" not in hover:
            raise ValueError("Hover object has no contents")

        parts = cls._normalize_hover_contents(hover["contents"])
        original_chars = sum(len(part["value"]) for part in parts)
        parts, returned_chars, truncated = (
            cls._truncate_hover_contents(parts, max_chars,)
        )
        result: dict[str, Any] = {
            "contents": parts,
            "content_chars": original_chars,
            "returned_content_chars": returned_chars,
            "content_truncated": truncated,
        }
        if hover.get("range") is not None:
            result["range"] = cls._range(hover["range"])

        return result

    @classmethod
    def _normalize_hover_contents(cls, contents: Any) -> list[dict[str, Any]]:
        """Normalize MarkedString, MarkedString[] or MarkupContent."""
        # ------------------------------------------------------------
        # Legacy MarkedString:
        #
        # "some markdown text"
        # ------------------------------------------------------------
        if isinstance(contents, str):
            return [{
                    "kind": "markdown",
                    "value": contents,
                }]

        # ------------------------------------------------------------
        # Either:
        #
        # MarkupContent:
        # {
        #   "kind": "markdown" | "plaintext",
        #   "value": "..."
        # }
        #
        # or legacy MarkedString:
        # {
        #   "language": "python",
        #   "value": "def foo(...)"
        # }
        # ------------------------------------------------------------
        if isinstance(contents, dict):
            return [cls._normalize_hover_part(contents)]

        # ------------------------------------------------------------
        # Legacy MarkedString[]
        #
        # Be slightly tolerant and accept normalized markup-like
        # dictionary entries from real-world servers as well.
        # ------------------------------------------------------------
        if isinstance(contents, list):
            parts: list[dict[str, Any]] = []
            for part in contents:
                if isinstance(part, str):
                    parts.append({
                            "kind": "markdown",
                            "value": part,
                        })
                elif isinstance(part, dict):
                    parts.append(
                        cls._normalize_hover_part(part)
                    )
                else:
                    raise ValueError("Invalid hover content item")
            return parts

        raise ValueError("Invalid hover contents")

    @staticmethod
    def _normalize_hover_part(part: dict[str, Any]) -> dict[str, Any]:
        # MarkupContent
        if "kind" in part:
            kind = part["kind"]
            value = part.get("value")
            if kind not in {"markdown", "plaintext"}:
                raise ValueError("Invalid hover markup kind")
            if not isinstance(value, str):
                raise ValueError("Invalid hover markup value")
            return {
                "kind": kind,
                "value": value,
            }

        # Legacy MarkedString object
        if "language" in part:
            language = part["language"]
            value = part.get("value")
            if not isinstance(language, str):
                raise ValueError("Invalid hover language")
            if not isinstance(value, str):
                raise ValueError("Invalid hover code value")
            return {
                "kind": "code",
                "language": language,
                "value": value,
            }

        raise ValueError("Invalid hover content object")

    @staticmethod
    def _truncate_hover_contents(
        parts: list[dict[str, Any]],
        max_chars: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Bound hover text while preserving part order."""
        remaining = max_chars
        result: list[dict[str, Any]] = []
        returned_chars = 0
        truncated = False

        for part in parts:
            value = part["value"]
            if remaining <= 0:
                truncated = True
                break
            if len(value) <= remaining:
                result.append(part)
                returned_chars += len(value)
                remaining -= len(value)
                continue

            clipped = dict(part)
            clipped["value"] = value[:remaining]
            clipped["truncated"] = True

            result.append(clipped)

            returned_chars += remaining
            remaining = 0
            truncated = True

        if len(result) < len(parts):
            truncated = True

        return result, returned_chars, truncated

# SearchWorkspaceSymbolsTool
class SearchWorkspaceSymbolsTool(LspToolBase):
    """Search workspace symbols using one configured language server.

    Workspace symbol search has no source document, so a server must be
    selected explicitly when more than one language server is configured.

    Native results preserve server ordering; the document-symbol fallback uses
    bounded source traversal and case-insensitive substring matching. Refine
    large result sets with a narrower query
    instead of paginated across fresh server sessions.
    """

    execution_kind = ExecutionKind.SANDBOXED_PROCESS

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        execution_allowed: bool = False,
        command: Sequence[str] | None = None,
        language_id: str | None = None,
        file_extensions: Sequence[str] | None = None,
        lsp_registry: LspRegistry | None = None,
        timeout_seconds: float | None = None,
        max_query_chars: int = 256,
        max_symbols: int = 100,
        max_output_chars: int = 51200,
        allow_external_locations: bool = False,
        max_workspace_files: int = 64,
        max_workspace_entries: int = 5_000,
        max_workspace_bytes: int = 8 * 1024 * 1024,
        max_file_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        super().__init__(
            workspace_root,
            execution_allowed=execution_allowed,
            command=command,
            language_id=language_id,
            file_extensions=file_extensions,
            lsp_registry=lsp_registry,
            timeout_seconds=timeout_seconds,
        )
        if type(allow_external_locations) is not bool:
            raise ValueError("allow_external_locations must be a boolean")
        for name, value in (
            ("max_query_chars", max_query_chars),
            ("max_symbols", max_symbols),
            ("max_output_chars", max_output_chars),
            ("max_workspace_files", max_workspace_files),
            ("max_workspace_entries", max_workspace_entries),
            ("max_workspace_bytes", max_workspace_bytes),
            ("max_file_bytes", max_file_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_query_chars = max_query_chars
        self.max_symbols = max_symbols
        self.max_output_chars = max_output_chars
        self.allow_external_locations = allow_external_locations
        self.max_workspace_files = max_workspace_files
        self.max_workspace_entries = max_workspace_entries
        self.max_workspace_bytes = max_workspace_bytes
        self.max_file_bytes = max_file_bytes
        # A server ID must identify one executable configuration. Languages may
        # share it and use different timeouts; workspace requests use the maximum.
        self._server_groups: dict[str, list[LspLanguageConfig]] = {}
        for config in self.lsp_registry.languages:
            group = self._server_groups.setdefault(config.server_id, [])
            if group and group[0].command != config.command:
                raise ValueError(f"Conflicting commands for server_id: {config.server_id}")
            group.append(config)

    @property
    def definition(self) -> ToolDefinition:
        server_ids = list(self._server_groups)

        return ToolDefinition(
            name="search_workspace_symbols",
            description=(
                "Search classes, functions, methods, variables and other named "
                "symbols across the workspace using a language server. "
                f"Configured support: {self.lsp_registry.description}. "
                "Use this when you know a symbol name or partial name but do not "
                "know which file contains it. "
                "Native results preserve language-server ordering. Servers without "
                "workspace search fall back to case-insensitive substring matching "
                "of document symbols in a bounded source scan. Check coverage: "
                "empty results do not prove absence from the entire workspace. "
                "Positions use 1-based lines and UTF-16 columns, with exclusive ends. "
                "If results are truncated, use a more specific query rather than "
                "requesting another page. "
                "When multiple language servers are configured, server_id is required."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": self.max_query_chars,
                        "description": (
                            "Symbol-name search query, for example "
                            "'AgentRuntime', 'run', or 'ToolRegistry'."
                        ),
                    },
                    "server_id": {
                        "type": "string",
                        **({"enum": server_ids} if server_ids else {}),
                        "description": (
                            "Language server to query. May be omitted when "
                            "only one language server is configured."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": self.max_symbols,
                        "default": self.max_symbols,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, Any]) -> ToolResult:
        if not isinstance(arguments, dict):
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        if set(arguments) - {"query", "server_id", "limit"}:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "Allowed arguments: query, server_id, limit.",
            )
        query = arguments.get("query")
        server_id = arguments.get("server_id")
        limit = arguments.get("limit", self.max_symbols)
        if (
            not isinstance(query, str)
            or not query.strip()
            or "\0" in query
            or len(query) > self.max_query_chars
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "query must be non-empty and contain no NUL; "
                f"maximum length is {self.max_query_chars} characters.",
            )
        query = query.strip()
        if server_id is not None and (not isinstance(server_id, str) or not server_id.strip()):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "server_id must be a non-empty string."
            )
        if type(limit) is not int or not 1 <= limit <= self.max_symbols:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "limit must be a integer between 1 and max_symbols.",
            )

        if not self.execution_allowed:
            return tool_error(
                ToolErrorCode.PERMISSION_DENIED,
                "Language servers require an isolated execution context.",
            )
        config_result = self._select_workspace_symbol_server(server_id)
        if isinstance(config_result, ToolResult):
            return config_result
        config = config_result

        deadline = time.monotonic() + (self.timeout_seconds or config.timeout_seconds)
        coverage = {
            "scanned_files": 0,
            "skipped_files": 0,
            "scan_truncated": False,
            "index_completeness": "unknown",
        }
        try:
            with self._client_session(config) as client:
                capability = client.capabilities.get("workspaceSymbolProvider")
                native = capability is True or isinstance(capability, dict)
                if not native:
                    coverage["index_completeness"] = "scanned_files_only"
                document_capability = client.capabilities.get("documentSymbolProvider")
                documents = document_capability is True or isinstance(document_capability, dict)
                if not native and not documents:
                    raise LspUnsupportedError(
                        "Server supports neither workspace nor document symbols"
                    )
                # The worker owns only one request. Prepare an index in this same
                # session instead of relying on documents opened by earlier calls.
                fallback = []
                if documents:
                    fallback = self._prepare_workspace(
                        client,
                        config,
                        query,
                        deadline,
                        coverage,
                        collect=not native,
                    )
                if native:
                    result = self._workspace_request(
                        client,
                        "workspace/symbol",
                        {"query": query},
                        deadline,
                    )
                    result = self._resolve_workspace_locations(client, result, limit, deadline)
                else:
                    result = fallback
                (
                    symbols,
                    total,
                    omitted_external,
                    omitted_protected,
                    unresolved_locations,
                ) = self._normalize_workspace_symbols(
                    result,
                    limit=limit,
                    allow_external_locations=self.allow_external_locations,
                )

        except LspTimeoutError:
            return tool_error("LSP_TIMEOUT", "Language server timed out; retry the request.")
        except LspUnsupportedError:
            return tool_error(
                "LSP_UNSUPPORTED",
                "Server supports neither workspace search nor the document-symbol fallback.",
            )
        except LspResponseError:
            return tool_error(
                "LSP_REQUEST_FAILED", "Language server rejected the workspace symbol request."
            )
        except FileNotFoundError:
            return tool_error(
                "LSP_UNAVAILABLE",
                f"Server {config.server_id} or source file is unavailable.",
            )
        except PermissionError:
            return tool_error(ToolErrorCode.PERMISSION_DENIED)
        except (ValueError, TypeError, KeyError):
            return tool_error(
                "LSP_INVALID_RESPONSE", "Server returned invalid workspace symbol response."
            )
        except (LspError, OSError):
            return tool_error(
                "LSP_ERROR",
                f"Server {config.server_id} failed. Check its installation and configuration.",
            )

        data = {
            "query": query,
            "language_ids": list(
                dict.fromkeys(c.language_id for c in self._server_groups[config.server_id])
            ),
            "server_id": config.server_id,
            "search_mode": "workspace" if native else "document_symbols",
            "coverage": coverage,
            "symbols": symbols,
            "total_symbols": total,
            "returned_symbols": len(symbols),
            "truncated": total > len(symbols),
            "omitted_external_symbols": omitted_external,
            "omitted_protected_symbols": omitted_protected,
            "unresolved_locations": unresolved_locations,
        }
        data = self._fit_workspace_output(data)
        if data is None:
            return tool_error(
                ToolErrorCode.OUTPUT_TOO_LARGE,
                "Workspace symbol metadata exceeds the configured output limit.",
            )
        return ToolResult(success=True, data=data)

    def _normalize_workspace_symbols(
        self,
        result: Any,
        *,
        limit: int,
        allow_external_locations: bool,
    ) -> tuple[list[dict[str, Any]], int, int, int, int]:
        if result is None:
            return [], 0, 0, 0, 0
        if not isinstance(result, list):
            raise ValueError("Expected workspace symbol list or null")

        symbols: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        omitted_external = 0
        omitted_protected = 0
        unresolved_locations = 0

        for raw_symbol in result:
            item, omission = self._normalize_workspace_symbol(
                raw_symbol, allow_external_locations=allow_external_locations
            )

            if omission == "external":
                omitted_external += 1
                continue
            if omission == "protected":
                omitted_protected += 1
                continue
            if item is None:
                continue
            # Do not include the result index in symbol identity.
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            if not item["location_resolved"]:
                unresolved_locations += 1
            # Preserve language-server order. It may represent relevance.
            if total < limit:
                item["index"] = total
                symbols.append(item)
            total += 1

        return symbols, total, omitted_external, omitted_protected, unresolved_locations

    def _normalize_workspace_symbol(
        self,
        symbol: Any,
        *,
        allow_external_locations: bool,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not isinstance(symbol, dict):
            raise ValueError("Invalid workspace symbol")

        name = symbol.get("name")
        kind = symbol.get("kind")
        if not isinstance(name, str) or not name.strip() or type(kind) is not int:
            raise ValueError("Invalid workspace symbol name or kind")

        location = symbol.get("location")
        if not isinstance(location, dict):
            raise ValueError("Invalid workspace symbol location")

        uri = location.get("uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("Invalid workspace symbol URI")

        item: dict[str, Any] = {
            "name": name,
            "kind": (_SYMBOL_KINDS[kind] if 1 <= kind < len(_SYMBOL_KINDS) else "unknown"),
        }

        # Optional symbol metadata.
        container_name = symbol.get("containerName")
        if container_name is not None:
            if not isinstance(container_name, str):
                raise ValueError("Invalid container name")
            item["container_name"] = container_name

        deprecated = symbol.get("deprecated")
        if deprecated is not None and type(deprecated) is not bool:
            raise ValueError("Invalid deprecated flag")

        tags = symbol.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or any(type(tag) is not int or tag <= 0 for tag in tags):
                raise ValueError("Invalid workspace symbol tags")

        # SymbolTag.Deprecated == 1.
        if deprecated is True or (isinstance(tags, list) and 1 in tags):
            item["deprecated"] = True

        # Range.
        if location.get("range") is not None:
            item["range"] = self._range(location["range"])
            item["location_resolved"] = True
        else:
            item["location_resolved"] = False

        # URI / workspace security policy.
        requested = self._file_uri_to_path(uri)
        if requested is None:
            if not allow_external_locations:
                return None, "external"
            item["uri"] = uri
            item["in_workspace"] = False
            return item, None
        try:
            resolved = requested.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("Invalid file URI") from error
        if is_credential_path(requested, resolved):
            return None, "protected"
        if resolved.is_relative_to(self.workspace_root):
            item["path"] = resolved.relative_to(self.workspace_root).as_posix()
            item["in_workspace"] = True
            return item, None
        if not allow_external_locations:
            return None, "external"

        item["uri"] = uri
        item["path"] = str(resolved)
        item["in_workspace"] = False

        return item, None

    def _select_workspace_symbol_server(
        self, server_id: str | None
    ) -> LspLanguageConfig | ToolResult:
        if not self._server_groups:
            return tool_error("LSP_UNAVAILABLE", "No language servers are configured.")
        if server_id is None:
            if len(self._server_groups) != 1:
                available = ", ".join(self._server_groups)
                return tool_error(
                    ToolErrorCode.INVALID_ARGUMENTS,
                    f"server_id is required. Available servers: {available}.",
                )
            server_id = next(iter(self._server_groups))
        group = self._server_groups.get(server_id)
        if group is None:
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, f"Unsupported server_id: {server_id}."
            )
        return LspLanguageConfig(
            server_id,
            group[0].language_id,
            tuple(suffix for config in group for suffix in config.file_extensions),
            group[0].command,
            max(config.timeout_seconds for config in group),
        )

    @staticmethod
    def _workspace_request(client, method: str, params: dict, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LspTimeoutError("Workspace symbol search exceeded its time budget")
        return client.request(method, params, timeout=remaining)

    def _workspace_files(self, server_id: str, coverage: dict, deadline: float):
        excluded = {".venv", "venv", "node_modules", "__pycache__", "build", "dist", "target"}
        coverage["excluded_directories"] = sorted(excluded)
        pending = [self.workspace_root]
        entries = 0
        while pending:
            directory = pending.pop()
            children = []
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        if time.monotonic() >= deadline:
                            raise LspTimeoutError("Workspace scan exceeded its time budget")
                        entries += 1
                        if entries > self.max_workspace_entries:
                            coverage["scan_truncated"] = True
                            return
                        children.append(Path(entry.path))
            except OSError:
                coverage["skipped_files"] += 1
                continue
            for path in sorted(children):
                if time.monotonic() >= deadline:
                    raise LspTimeoutError("Workspace scan exceeded its time budget")
                try:
                    if is_protected_name(path):
                        continue
                    if path.is_dir():
                        if (
                            path.name not in excluded
                            and not path.is_symlink()
                            and not is_credential_path(path, path.resolve())
                        ):
                            pending.append(path)
                        continue
                    config = self.lsp_registry.select(path)
                    if config is not None and config.server_id == server_id:
                        yield path
                except (OSError, RuntimeError):
                    coverage["skipped_files"] += 1

    def _prepare_workspace(self, client, config, query, deadline, coverage, *, collect):
        matches = []
        seen_paths = set()
        total_bytes = 0
        attempted = 0
        for path in self._workspace_files(config.server_id, coverage, deadline):
            if attempted >= self.max_workspace_files:
                coverage["scan_truncated"] = True
                break
            attempted += 1
            snapshot = self._read_snapshot(self.lsp_registry, self.workspace_root, str(path))
            if isinstance(snapshot, ToolResult):
                coverage["skipped_files"] += 1
                continue
            target, language, raw, text = snapshot
            if language.server_id != config.server_id:
                coverage["skipped_files"] += 1
                continue
            if target in seen_paths:
                continue
            seen_paths.add(target)
            if total_bytes + len(raw) > self.max_workspace_bytes:
                coverage["scan_truncated"] = True
                break
            total_bytes += len(raw)
            try:
                uri = client.sync_document(target, text=text, language_id=language.language_id)
                result = self._workspace_request(
                    client,
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": uri}},
                    deadline,
                )
            except (LspResponseError, LspUnsupportedError):
                coverage["skipped_files"] += 1
                continue
            if self._verify_snapshot(target, raw, self.max_file_bytes) is not None:
                coverage["skipped_files"] += 1
                continue
            coverage["scanned_files"] += 1
            if collect:
                for symbol in self._document_workspace_symbols(result, uri):
                    if query.casefold() in symbol["name"].casefold():
                        matches.append(symbol)
                        if len(matches) >= 10_000:
                            coverage["scan_truncated"] = True
                            return matches
        return matches

    @classmethod
    def _document_workspace_symbols(cls, result, uri):
        if result is None:
            return
        if not isinstance(result, list):
            raise ValueError("Expected document symbol list")
        hierarchical = bool(result and isinstance(result[0], dict) and "location" not in result[0])
        exhausted = object()
        stack = [(iter(result), None)]
        while stack:
            iterator, container = stack[-1]
            symbol = next(iterator, exhausted)
            if symbol is exhausted:
                stack.pop()
                continue
            if not isinstance(symbol, dict) or ("location" not in symbol) != hierarchical:
                raise ValueError("Invalid or mixed document symbol formats")
            name, kind = symbol.get("name"), symbol.get("kind")
            if not isinstance(name, str) or not name.strip() or type(kind) is not int:
                raise ValueError("Invalid document symbol name or kind")
            item = {k: symbol[k] for k in ("name", "kind", "tags", "deprecated") if k in symbol}
            if hierarchical:
                cls._range(symbol["range"])
                cls._range(symbol["selectionRange"])
                item["location"] = {"uri": uri, "range": symbol["selectionRange"]}
                if container is not None:
                    item["containerName"] = container
                children = symbol.get("children", [])
                if not isinstance(children, list):
                    raise ValueError("Invalid symbol children")
                stack.append((iter(children), name))
            else:
                location = symbol.get("location")
                if not isinstance(location, dict) or location.get("uri") != uri:
                    raise ValueError("Document symbol refers to another file")
                cls._range(location["range"])
                item["location"] = location
                if "containerName" in symbol:
                    item["containerName"] = symbol["containerName"]
            yield item

    def _resolve_workspace_locations(self, client, result, limit, deadline):
        if result is None:
            return None
        if not isinstance(result, list):
            raise ValueError("Expected workspace symbol list")
        capability = client.capabilities.get("workspaceSymbolProvider")
        if not isinstance(capability, dict) or capability.get("resolveProvider") is not True:
            return result
        resolved = list(result)
        seen = set()
        cache = {}
        for index, symbol in enumerate(result):
            item, omission = self._normalize_workspace_symbol(
                symbol,
                allow_external_locations=self.allow_external_locations,
            )
            if omission or item is None:
                continue
            # Keep opaque identity for unresolved overloads, and reuse a resolved
            # response for duplicate raw symbols so normalization can deduplicate.
            identity = json.dumps(symbol, sort_keys=True, ensure_ascii=False)
            if identity in cache:
                resolved[index] = cache[identity]
                continue
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > limit:
                continue
            if not item["location_resolved"]:
                try:
                    # Forward the original symbol, including opaque server data.
                    response = self._workspace_request(
                        client,
                        "workspaceSymbol/resolve",
                        symbol,
                        deadline,
                    )
                except LspResponseError:
                    continue
                self._normalize_workspace_symbol(
                    response,
                    allow_external_locations=self.allow_external_locations,
                )
                resolved[index] = response
                cache[identity] = response
        return resolved

    def _fit_workspace_output(self, data):
        def fits(candidate):
            return len(json.dumps(candidate, ensure_ascii=False)) <= self.max_output_chars

        data = {**data, "omitted_oversized_symbols": 0}
        if fits(data):
            return data
        symbols = data["symbols"]

        def prefix(items, omitted):
            return {
                **data,
                "symbols": items,
                "returned_symbols": len(items),
                "truncated": data["total_symbols"] > len(items),
                "omitted_oversized_symbols": omitted,
            }

        # Never silently change an identifier/path to fit. Omit individually
        # oversized symbols and report their count, allowing later matches through.
        kept = []
        omitted = 0
        for symbol in symbols:
            if fits(prefix([symbol], 0)):
                kept.append(symbol)
            else:
                omitted += 1
        # Counter digit growth can make another single result too large.
        while True:
            fitting = [symbol for symbol in kept if fits(prefix([symbol], omitted))]
            if len(fitting) == len(kept):
                break
            omitted += len(kept) - len(fitting)
            kept = fitting
        best = prefix([], omitted)
        if not fits(best):
            return None
        low, high = 0, len(kept)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = prefix(kept[:middle], omitted)
            if fits(candidate):
                low, best = middle, candidate
            else:
                high = middle - 1
        return best
