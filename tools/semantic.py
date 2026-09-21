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
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from llm import ToolDefinition

from .base import ToolResult
from .errors import ToolErrorCode, tool_error
from .file_policy import is_credential_path
from .lsp_client import LspClient, LspError, LspResponseError, LspTimeoutError, LspUnsupportedError
from .lsp_config import LspLanguageConfig, LspRegistry, default_lsp_registry

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
        max_output_chars: int = 20_000,
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
        max_output_chars: int = 20_000,
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
        max_output_chars: int = 20_000,
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
            "path",
            "line",
            "column",
            "include_declaration",
            "start_index",
            "limit",
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
        max_output_chars: int = 20_000,
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
