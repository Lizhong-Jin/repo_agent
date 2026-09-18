"""Deterministic subprocess peer for testing the actual LSP byte transport."""

import json
import sys
import time

mode = sys.argv[1]
documents = {}


def send(message):
    message = {"jsonrpc": "2.0", **message}
    body = json.dumps(message, ensure_ascii=False).encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    # Deliberate partial writes exercise framing independent of pipe boundaries.
    for offset in range(0, len(frame), 7):
        sys.stdout.buffer.write(frame[offset : offset + 7])
        sys.stdout.buffer.flush()


while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method = message.get("method")
    params = message.get("params")
    if method == "initialize":
        if mode == "crash":
            sys.exit(9)
        if mode == "malformed":
            sys.stdout.buffer.write(b"Content-Length: nope\r\n\r\n")
            sys.stdout.buffer.flush()
            continue
        if mode == "hang":
            time.sleep(60)
        capabilities = {
            "textDocumentSync": {"openClose": True, "change": 2},
            "documentSymbolProvider": {},
            "definitionProvider": True,
            "referencesProvider": True,
            "workspaceSymbolProvider": True,
        }
        if mode == "pull":
            capabilities["diagnosticProvider"] = {"identifier": "test"}
        if mode == "unsupported":
            capabilities.pop("definitionProvider")
        send({"id": message["id"], "result": {"capabilities": capabilities}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        doc = params["textDocument"]
        uri = doc["uri"]
        documents[uri] = params
        if mode not in ("silent", "pull"):
            # A stale empty report must never mask the current error.
            send(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": uri,
                        "version": doc["version"] - 1,
                        "diagnostics": [],
                    },
                }
            )
            report = {"uri": uri, "diagnostics": [{"message": "类型错误 😀"}]}
            if mode != "unversioned":
                report["version"] = doc["version"]
            send({"method": "textDocument/publishDiagnostics", "params": report})
    elif method == "textDocument/documentSymbol":
        symbols = [{"name": "函数😀"}]
        if mode == "tool-symbols":
            span = {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 8}}
            symbols = [{"name": "example", "kind": 12, "range": span, "selectionRange": span}]
        send({"id": message["id"], "result": symbols})
    elif method == "textDocument/definition":
        send(
            {
                "id": message["id"],
                "result": {
                    "uri": params["textDocument"]["uri"],
                    "range": {
                        "start": params["position"],
                        "end": params["position"],
                    },
                },
            }
        )
    elif method == "textDocument/references":
        send({"id": message["id"], "result": []})
    elif method == "textDocument/diagnostic":
        assert params["identifier"] == "test"
        send({"id": message["id"], "result": {"kind": "full", "items": []}})
    elif method == "test/documents":
        send({"id": message["id"], "result": documents})
    elif method == "test/config":
        original_id = message["id"]
        send(
            {
                "id": "config",
                "method": "workspace/configuration",
                "params": {
                    "items": [{"section": "python.analysis"}, {"section": "missing"}],
                },
            }
        )
    elif message.get("id") == "config":
        send({"id": original_id, "result": message["result"]})
    elif method == "test/stderr":
        sys.stderr.write("x" * 100000)
        sys.stderr.flush()
        send({"id": message["id"], "result": True})
    elif method == "test/hang":
        time.sleep(60)
    elif method == "test/stop-reading":
        send({"id": message["id"], "result": True})
        time.sleep(60)
    elif method == "test/error":
        send(
            {
                "id": message["id"],
                "error": {
                    "code": -32602,
                    "message": "bad arguments",
                    "data": {"detail": 1},
                },
            }
        )
    elif method == "shutdown":
        send({"id": message["id"], "result": None})
    elif method == "exit":
        sys.exit(0)
    elif "id" in message:
        send({"id": message["id"], "result": None})
