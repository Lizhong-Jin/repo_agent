"""Workspace code-analysis tools backed by a trusted language server.

Like command tools, these require an isolated execution context. Path checks do
not restrict the language server's own filesystem access. Each default call owns
and closes its client, matching the current single-request sandbox worker.
"""

import hashlib
import json
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from llm import ToolDefinition

from .base import ToolResult
from .errors import ToolErrorCode, tool_error
from .file_policy import is_credential_path
from .lsp_client import LspClient, LspError, LspResponseError, LspTimeoutError, LspUnsupportedError
from .lsp_config import LspLanguageConfig, LspRegistry, default_lsp_registry

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


class GetSymbolsTool:
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
        if type(execution_allowed) is not bool:
            raise ValueError("execution_allowed must be a boolean")
        for name, value in (
            ("max_file_bytes", max_file_bytes),
            ("max_symbols", max_symbols),
            ("max_output_chars", max_output_chars),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
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
        self.max_file_bytes = max_file_bytes
        self.max_symbols = max_symbols
        self.max_output_chars = max_output_chars

    def _new_client(self, config: LspLanguageConfig) -> LspClient:
        return LspClient(
            self.workspace_root,
            config.command,
            language_id=config.language_id,
            timeout_seconds=(
                self.timeout_seconds if self.timeout_seconds is not None else config.timeout_seconds
            ),
        )

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
        if not isinstance(arguments, dict) or set(arguments) - {"path", "start_index", "limit"}:
            return tool_error(ToolErrorCode.INVALID_ARGUMENTS)
        path = arguments.get("path")
        start = arguments.get("start_index", 0)
        limit = arguments.get("limit", self.max_symbols)
        if (
            not isinstance(path, str)
            or not path.strip()
            or "\0" in path
            or type(start) is not int
            or start < 0
            or type(limit) is not int
            or not 1 <= limit <= self.max_symbols
        ):
            return tool_error(
                ToolErrorCode.INVALID_ARGUMENTS, "Invalid path, start_index or limit."
            )
        try:
            target = (self.workspace_root / path).resolve()
            if is_credential_path(self.workspace_root / path, target):
                return tool_error(ToolErrorCode.PROTECTED_FILE)
            if not target.is_relative_to(self.workspace_root):
                return tool_error(ToolErrorCode.PATH_OUTSIDE_WORKSPACE)
            if not stat.S_ISREG(target.stat().st_mode):
                return tool_error(ToolErrorCode.NOT_A_FILE)
            config = self.lsp_registry.select(target)
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

        try:
            with self._new_client(config) as client:
                capability = client.capabilities.get("documentSymbolProvider")
                if capability is not True and not isinstance(capability, dict):
                    raise LspUnsupportedError("Server does not support document symbols")
                # Send precisely the validated snapshot, without re-reading via get_symbols().
                uri = client.sync_document(target, text=text)
                symbols = client.request(
                    "textDocument/documentSymbol", {"textDocument": {"uri": uri}}
                )
            page, total, hierarchy = self._normalize(symbols, target.as_uri(), start, limit)
            with target.open("rb") as stream:
                if stream.read(self.max_file_bytes + 1) != raw:
                    return tool_error(
                        ToolErrorCode.FILE_CHANGED, "File changed during analysis; retry."
                    )
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
                f"Server {config.server_id} or source file is unavailable. "
                "Rebuild the sandbox image.",
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

    @classmethod
    def _normalize(cls, symbols: Any, uri: str, start: int, limit: int) -> tuple[list, int, bool]:
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
