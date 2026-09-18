import sys
import time
from pathlib import Path

import pytest

from tools.lsp_client import (
    LspClient,
    LspError,
    LspResponseError,
    LspTimeoutError,
    LspUnsupportedError,
)

SERVER = Path(__file__).parent / "fixtures" / "lsp_server.py"


def client(root, mode="push", **kwargs):
    return LspClient(root, [sys.executable, str(SERVER), mode], language_id="python", **kwargs)


def test_queries_unicode_framing_and_incremental_sync(tmp_path):
    path = tmp_path / "代码 空格.py"
    path.write_text("a\r\nb😀", encoding="utf-8")
    with client(tmp_path) as connection:
        assert connection.get_symbols(path) == [{"name": "函数😀"}]
        assert connection.go_to_definition(path, 1, 3)[0]["uri"] == path.as_uri()
        assert connection.find_references(path, 1, 0) == []
        path.write_text("changed", encoding="utf-8")
        connection.get_symbols(path)
        doc = connection.request("test/documents")[path.as_uri()]
        assert doc["textDocument"]["version"] == 2
        assert doc["contentChanges"] == [
            {
                "text": "changed",
                "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 1, "character": 3},
                },
            }
        ]
        connection.sync_document(path)
        assert connection.request("test/documents")[path.as_uri()] == doc
    assert connection._process.poll() is not None
    assert all(not t.is_alive() for t in connection._threads)
    connection.close()
    with pytest.raises(LspError):
        connection.start()


@pytest.mark.parametrize("mode", ["push", "pull", "unversioned"])
def test_diagnostics_report_freshness(tmp_path, mode):
    path = tmp_path / "a.py"
    path.write_text("a")
    with client(tmp_path, mode) as connection:
        first = connection.get_diagnostics(path)
        assert first["version"] == (None if mode == "unversioned" else 1)
        assert first["items"] == ([] if mode == "pull" else [{"message": "类型错误 😀"}])
        path.write_text("b")
        second = connection.get_diagnostics(path)
        assert second["version"] == (None if mode == "unversioned" else 2)
        assert second["source"] == ("pull" if mode == "pull" else "push")


def test_no_diagnostics_is_timeout_not_empty_success(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("a")
    connection = client(tmp_path, "silent", timeout_seconds=0.3)
    with pytest.raises(LspTimeoutError):
        connection.get_diagnostics(path)
    assert connection._process.poll() is not None


@pytest.mark.parametrize("mode", ["crash", "malformed", "hang"])
def test_initialization_failure_cleans_up(tmp_path, mode):
    connection = client(tmp_path, mode, timeout_seconds=0.3)
    start = time.monotonic()
    with pytest.raises(LspError):
        connection.start()
    assert time.monotonic() - start < 4
    assert connection._process.poll() is not None


def test_configuration_requests_errors_and_stderr_drain(tmp_path):
    with client(tmp_path, settings={"python": {"analysis": {"enabled": True}}}) as connection:
        assert connection.request("test/config") == [{"enabled": True}, None]
        assert connection.request("test/stderr") is True
        assert len(connection.stderr) <= 32768
        with pytest.raises(LspResponseError) as caught:
            connection.request("test/error")
        assert caught.value.code == -32602
        assert caught.value.data == {"detail": 1}
        assert connection.request("test/documents") == {}


def test_request_timeout_terminates_session(tmp_path):
    with client(tmp_path, timeout_seconds=0.3) as connection:
        with pytest.raises(LspTimeoutError):
            connection.request("test/hang")
        assert connection._process.poll() is not None


def test_blocked_stdin_has_bounded_timeout(tmp_path):
    with client(tmp_path, timeout_seconds=0.3) as connection:
        connection.request("test/stop-reading")
        started = time.monotonic()
        with pytest.raises(LspTimeoutError):
            connection.request("test/large", {"text": "x" * 1_000_000})
        assert time.monotonic() - started < 4
        assert all(not thread.is_alive() for thread in connection._threads)


def test_capabilities_and_path_validation(tmp_path):
    with client(tmp_path, "unsupported") as connection:
        with pytest.raises(LspUnsupportedError):
            connection.go_to_definition("missing.py", 0, 0)
        with pytest.raises(ValueError):
            connection.sync_document("../outside.py", text="")
        connection.sync_document("a.py", text="a")
        connection.close_document("a.py")
        assert connection._documents == {}


@pytest.mark.parametrize("command", ["python", [], [""], [None], ["bad\0command"]])
def test_invalid_command(tmp_path, command):
    with pytest.raises(ValueError):
        LspClient(tmp_path, command, language_id="python")


def test_missing_executable(tmp_path):
    connection = LspClient(tmp_path, [str(tmp_path / "missing")], language_id="python")
    with pytest.raises(OSError):
        connection.start()
    connection.close()
