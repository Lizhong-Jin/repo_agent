"""Synchronous stdio LSP client, independent of tool schemas and LLM types.

Use one client per workspace/server, and close it (preferably with ``with``).
Positions and returned data use LSP conventions: zero-based lines, UTF-16 code
units, file URIs and raw Location/LocationLink/DocumentSymbol dictionaries.
Queries synchronize the requested file from disk; callers must explicitly sync
other changed files. Server commands are trusted configuration, not model input.
This module is not a sandbox: cwd/path validation does not confine the server.
"""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from host_support.cancellation import RunCancelled, checkpoint, current_cancellation
from host_support.processes import kill_process_group, start_process

logger = logging.getLogger(__name__)


class LspError(RuntimeError):
    """Transport, lifecycle or protocol failure."""


class LspTimeoutError(LspError):
    """No response before the deadline; the session is closed to discard stale state."""


class LspResponseError(LspError):
    """JSON-RPC error returned by the server."""

    def __init__(self, error: dict[str, Any]):
        super().__init__(str(error.get("message", "LSP request failed")))
        self.code = error.get("code")
        self.data = error.get("data")


class LspUnsupportedError(LspError):
    """The server did not advertise a required capability."""


class LspClient:
    """One serial, reusable server session; public operations are serialized.

    ``env`` replaces the inherited environment when supplied. ``settings`` serves
    workspace/configuration requests. Servers using dynamic registration are not
    supported; this client explicitly advertises static capabilities only.
    """

    def __init__(
        self,
        workspace_root: str | Path,
        command: Sequence[str],
        *,
        language_id: str,
        timeout_seconds: float = 10,
        env: Mapping[str, str] | None = None,
        settings: Mapping[str, Any] | None = None,
        initialization_options: Any = None,
        max_message_bytes: int = 16 * 1024 * 1024,
    ):
        if (
            isinstance(command, (str, bytes))
            or not isinstance(command, Sequence)
            or not command
            or any(not isinstance(x, str) or "\0" in x for x in command)
            or not command[0]
        ):
            raise ValueError("command must be an argv sequence with a non-empty executable")
        if not isinstance(language_id, str) or not language_id:
            raise ValueError("language_id must be a non-empty string")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if type(max_message_bytes) is not int or max_message_bytes <= 0:
            raise ValueError("max_message_bytes must be a positive integer")
        self.root = Path(workspace_root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace_root must be a directory")
        self.command = list(command)
        self.language_id = language_id
        self.timeout_seconds = timeout_seconds
        self.env = dict(env) if env is not None else None
        self.settings = copy.deepcopy(dict(settings or {}))
        self.initialization_options = copy.deepcopy(initialization_options)
        self.max_message_bytes = max_message_bytes
        self.capabilities: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition()
        self._process: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._outgoing: queue.Queue = queue.Queue(maxsize=128)
        self._responses: dict[int, Any] = {}
        self._documents: dict[str, tuple[int, str]] = {}
        self._diagnostics: dict[str, dict[str, Any]] = {}
        self._failure: LspError | None = None
        self._next_id = 0
        self._ready = False
        self._closed = False
        self._stderr = bytearray()

    @property
    def stderr(self) -> str:
        """Bounded tail of server stderr, useful when startup fails."""
        with self._condition:
            return self._stderr.decode("utf-8", errors="replace")

    def __enter__(self) -> LspClient:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def start(self) -> None:
        """Start and initialize once. A closed/failed client cannot be restarted."""
        with self._lock:
            checkpoint()
            if self._closed:
                raise LspError("Client is closed; create a new client")
            if self._ready:
                self._check_failure()
                return
            try:
                self._process = start_process(
                    self.command,
                    cwd=self.root,
                    env=self.env,
                    shell=False,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    windows_group=False,
                )
                for target in (self._read_loop, self._write_loop, self._stderr_loop):
                    thread = threading.Thread(target=target, daemon=True)
                    self._threads.append(thread)
                    thread.start()
                result = self._request(
                    "initialize",
                    {
                        "processId": os.getpid(),
                        "rootUri": self.root.as_uri(),
                        "workspaceFolders": self._workspace_folders(),
                        "capabilities": {
                            "general": {"positionEncodings": ["utf-16"]},
                            "workspace": {
                                "configuration": True,
                                "workspaceFolders": True,
                                "symbol": {
                                    "dynamicRegistration": False,
                                    "symbolKind": {"valueSet": list(range(1, 27))},
                                    "tagSupport": {"valueSet": [1]},
                                    "resolveSupport": {"properties": ["location.range"]},
                                },
                            },
                            "textDocument": {
                                "synchronization": {"dynamicRegistration": False},
                                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                "definition": {"linkSupport": True},
                                "references": {"dynamicRegistration": False},
                                "publishDiagnostics": {"versionSupport": True},
                                "diagnostic": {"dynamicRegistration": False},
                                "hover": {
                                    "dynamicRegistration": False,
                                    "contentFormat": ["markdown", "plaintext"],
                                },
                            },
                        },
                        "initializationOptions": self.initialization_options,
                    },
                )
                if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
                    raise LspError("Invalid initialize result")
                self.capabilities = result["capabilities"]
                if self.capabilities.get("positionEncoding", "utf-16") != "utf-16":
                    raise LspUnsupportedError("Only UTF-16 positions are supported")
                self._notify("initialized", {})
                if self.settings:
                    self._notify("workspace/didChangeConfiguration", {"settings": self.settings})
                self._ready = True
            except BaseException:
                self._dispose()
                raise

    def request(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        """Escape hatch for additional LSP requests; returns unmodified JSON data."""
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
        with self._lock:
            self.start()
            return self._request(method, params, timeout=timeout)

    def sync_document(
        self,
        path: str | Path,
        *,
        text: str | None = None,
        language_id: str | None = None,
    ) -> str:
        """Open/update a UTF-8 file or supplied buffer; return its file URI.

        Full replacement ranges are used for incremental-sync servers. Supplying
        text does not write it to disk. language_id selects the language when
        opening a document on a server shared by multiple languages. Query
        helpers subsequently read disk.
        """
        if language_id is not None and (not isinstance(language_id, str) or not language_id):
            raise ValueError("language_id must be a non-empty string")
        with self._lock:
            self.start()
            target = (self.root / path).resolve()
            if not target.is_relative_to(self.root):
                raise ValueError("Document path must stay inside workspace")
            if text is None:
                with target.open("rb") as stream:
                    raw = stream.read(self.max_message_bytes + 1)
                if len(raw) > self.max_message_bytes:
                    raise ValueError("Document exceeds size limit")
                text = raw.decode("utf-8")
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            uri = target.as_uri()
            sync = self.capabilities.get("textDocumentSync", 0)
            kind = sync.get("change", 0) if isinstance(sync, dict) else sync
            open_close = sync.get("openClose", False) if isinstance(sync, dict) else kind != 0
            if not open_close:
                raise LspUnsupportedError("Server does not support document open/close sync")
            with self._condition:
                previous = self._documents.get(uri)
                if previous and previous[1] == text:
                    return uri
                version = previous[0] + 1 if previous else 1
                if previous:
                    if kind not in (1, 2):
                        raise LspUnsupportedError("Server does not support document changes")
                    change: dict[str, Any] = {"text": text}
                    if kind == 2:
                        change["range"] = {
                            "start": {"line": 0, "character": 0},
                            "end": self._end_position(previous[1]),
                        }
                    method = "textDocument/didChange"
                    params = {
                        "textDocument": {"uri": uri, "version": version},
                        "contentChanges": [change],
                    }
                else:
                    method = "textDocument/didOpen"
                    params = {
                        "textDocument": {
                            "uri": uri,
                            "languageId": (
                                language_id if language_id is not None else self.language_id
                            ),
                            "version": version,
                            "text": text,
                        }
                    }
                self._notify(method, params)
                self._documents[uri] = (version, text)
                self._diagnostics.pop(uri, None)
            return uri

    def close_document(self, path: str | Path) -> None:
        with self._lock:
            uri = (self.root / path).resolve().as_uri()
            with self._condition:
                if uri in self._documents:
                    self._notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                    del self._documents[uri]
                    self._diagnostics.pop(uri, None)

    def get_symbols(self, path: str | Path) -> list:
        with self._lock:
            self._require("documentSymbolProvider")
            uri = self.sync_document(path)
            return (
                self._request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}) or []
            )

    def workspace_symbols(self, query: str) -> list:
        with self._lock:
            self._require("workspaceSymbolProvider")
            return self._request("workspace/symbol", {"query": query}) or []

    def go_to_definition(self, path: str | Path, line: int, character: int) -> list:
        with self._lock:
            self._require("definitionProvider")
            result = self._request("textDocument/definition", self._position(path, line, character))
            return result if isinstance(result, list) else [result] if result else []

    def find_references(
        self,
        path: str | Path,
        line: int,
        character: int,
        *,
        include_declaration: bool = True,
    ) -> list:
        with self._lock:
            self._require("referencesProvider")
            params = self._position(path, line, character)
            params["context"] = {"includeDeclaration": include_declaration}
            return self._request("textDocument/references", params) or []

    def get_diagnostics(
        self,
        path: str | Path,
        *,
        text: str | None = None,
    ) -> dict[str, Any]:
        """Return diagnostics plus freshness metadata; absence is never success.

        When text is supplied, diagnose exactly that snapshot instead of
        re-reading the document from disk.

        Prefer pull diagnostics when advertised. For push servers, wait for a
        report. ``version=None`` means freshness cannot be verified: unversioned
        notifications may describe older content. Cross-file dependency freshness
        is not guaranteed even for versioned reports. Timeout closes the session.
        """
        with self._lock:
            uri = self.sync_document(path, text=text)
            version = self._documents[uri][0]
            if self._supports("diagnosticProvider"):
                params: dict[str, Any] = {"textDocument": {"uri": uri}}
                provider = self.capabilities["diagnosticProvider"]
                if isinstance(provider, dict) and "identifier" in provider:
                    params["identifier"] = provider["identifier"]
                report = self._request("textDocument/diagnostic", params)
                if not isinstance(report, dict) or report.get("kind") != "full":
                    raise LspError("Expected full diagnostics (no previousResultId was sent)")
                return {"uri": uri, "version": version, "source": "pull", "items": report["items"]}
            deadline = time.monotonic() + self.timeout_seconds
            try:
                with self._condition:
                    while uri not in self._diagnostics:
                        checkpoint()
                        self._check_failure()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LspTimeoutError("Timed out waiting for published diagnostics")
                        self._condition.wait(min(remaining, 0.05))
                    return copy.deepcopy(self._diagnostics[uri])
            except (LspError, RunCancelled):
                self._dispose()
                raise

    def close(self) -> None:
        """Best-effort shutdown/exit, then bounded process and pipe cleanup."""
        with self._lock:
            if self._closed:
                return
            try:
                context = current_cancellation()
                if context is not None and context.event.is_set():
                    return
                if self._ready and self._failure is None:
                    self._request("shutdown", None, timeout=min(self.timeout_seconds, 1))
                    self._notify("exit", None)
                    if self._process:
                        self._process.wait(timeout=1)
            except (LspError, subprocess.TimeoutExpired):
                pass
            finally:
                self._dispose()

    def _supports(self, capability: str) -> bool:
        value = self.capabilities.get(capability)
        return value is True or isinstance(value, dict)

    def _require(self, capability: str) -> None:
        self.start()
        if not self._supports(capability):
            raise LspUnsupportedError(f"Server does not advertise {capability}")

    def _position(self, path: str | Path, line: int, character: int) -> dict:
        if any(type(x) is not int or x < 0 for x in (line, character)):
            raise ValueError("line and character must be zero-based non-negative integers")
        return {
            "textDocument": {"uri": self.sync_document(path)},
            "position": {"line": line, "character": character},
        }

    @staticmethod
    def _end_position(text: str) -> dict[str, int]:
        # LSP recognizes CRLF, LF and CR as line separators.
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return {"line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}

    def _workspace_folders(self) -> list:
        return [{"uri": self.root.as_uri(), "name": self.root.name}]

    def _notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, message: dict) -> None:
        self._check_failure()
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(payload) > self.max_message_bytes:
            raise LspError("Outgoing message exceeds size limit")
        frame = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        try:
            self._outgoing.put_nowait(frame)
        except queue.Full as error:
            raise LspError("Server write queue is full") from error

    def _request(self, method: str, params: Any, *, timeout: float | None = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout_seconds)
        try:
            with self._condition:
                self._responses[request_id] = None
                self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                while self._responses[request_id] is None:
                    checkpoint()
                    self._check_failure()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LspTimeoutError(f"Timed out waiting for {method}")
                    self._condition.wait(min(remaining, 0.05))
                response = self._responses[request_id]
            if "error" in response:
                raise LspResponseError(response["error"])
            return response["result"]
        except LspResponseError:
            raise
        except (LspError, RunCancelled):
            self._dispose()
            raise
        finally:
            with self._condition:
                self._responses.pop(request_id, None)

    def _check_failure(self) -> None:
        if self._failure:
            raise self._failure
        if self._closed:
            raise LspError("Client is closed")

    def _fail(self, error: Exception) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = LspError(f"Language server connection failed: {error}")
            self._condition.notify_all()

    def _write_loop(self) -> None:
        try:
            while (frame := self._outgoing.get()) is not None:
                self._process.stdin.write(frame)
                self._process.stdin.flush()
        except (OSError, ValueError) as error:
            self._fail(error)

    def _stderr_loop(self) -> None:
        try:
            while chunk := self._process.stderr.read1(4096):
                with self._condition:
                    self._stderr.extend(chunk)
                    del self._stderr[:-32768]
        except (OSError, ValueError):
            pass

    def _read_loop(self) -> None:
        try:
            stream = self._process.stdout
            while True:
                headers = {}
                size = 0
                while True:
                    line = stream.readline(8193)
                    if not line:
                        raise LspError("Server closed stdout")
                    size += len(line)
                    if size > 8192 or not line.endswith(b"\r\n"):
                        raise LspError("Invalid or oversized LSP header")
                    if line == b"\r\n":
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    key = key.lower()
                    if key in headers:
                        raise LspError("Duplicate LSP header")
                    headers[key] = value.strip()
                length = int(headers["content-length"])
                if not 0 < length <= self.max_message_bytes:
                    raise LspError("Invalid or oversized LSP message")
                body = stream.read(length)
                if len(body) != length:
                    raise LspError("Truncated LSP message")
                message = json.loads(body.decode("utf-8"))
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise LspError("Invalid JSON-RPC message")
                self._dispatch(message)
        except Exception as error:
            self._fail(error)

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:
                self._server_request(message)
            elif message["method"] == "textDocument/publishDiagnostics":
                params = message["params"]
                uri = params["uri"]
                with self._condition:
                    document = self._documents.get(uri)
                    version = params.get("version")
                    if document and (version is None or version == document[0]):
                        self._diagnostics[uri] = {
                            "uri": uri,
                            "version": version,
                            "source": "push",
                            "items": params["diagnostics"],
                        }
                        self._condition.notify_all()
        else:
            if ("result" in message) == ("error" in message):
                raise LspError("Invalid JSON-RPC response")
            with self._condition:
                if message.get("id") in self._responses:
                    self._responses[message["id"]] = message
                    self._condition.notify_all()

    def _server_request(self, message: dict) -> None:
        response = {"jsonrpc": "2.0", "id": message["id"]}
        method = message["method"]
        if method == "workspace/configuration":
            values = []
            for item in message.get("params", {}).get("items", []):
                value: Any = self.settings
                for key in item.get("section", "").split(".") if item.get("section") else []:
                    value = value.get(key) if isinstance(value, dict) else None
                values.append(value)
            response["result"] = values
        elif method == "workspace/workspaceFolders":
            response["result"] = self._workspace_folders()
        elif method == "window/showMessageRequest":
            response["result"] = None
        else:
            # In particular, never apply server-initiated workspace edits.
            response["error"] = {"code": -32601, "message": f"Unsupported method: {method}"}
        self._send(response)

    def _dispose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        process = self._process
        if process is not None:
            try:
                kill_process_group(process)
            except ProcessLookupError:
                pass
            except OSError:
                # Some hosts deny killpg even when the leader has already exited.
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        logger.warning("Unable to terminate the language server", exc_info=True)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                logger.warning("Language server did not exit during cleanup")
        try:
            self._outgoing.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            thread.join(timeout=0.5)
        if process is not None:
            # Do not close a buffered stream while an escaped descendant keeps
            # its reader blocked: close() would wait on the buffer's lock.
            for stream, thread in zip(
                (process.stdout, process.stdin, process.stderr),
                self._threads,
                strict=False,
            ):
                if not thread.is_alive():
                    stream.close()
        context = current_cancellation()
        if context is not None and context.event.is_set() and process is not None:
            # This legacy LSP lifecycle does not supervise escaped descendants.
            context.record_cleanup(
                "unknown",
                pid=process.pid,
                error="Language-server descendant cleanup is not guaranteed.",
            )
        self._documents.clear()
        self._diagnostics.clear()
