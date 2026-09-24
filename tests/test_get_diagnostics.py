"""Diagnostic normalization, report freshness and actual stdio integration."""

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from tools import GetDiagnosticsTool
from tools._internal.lsp_client import LspTimeoutError

SPAN = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
SERVER = Path(__file__).parent / "fixtures" / "lsp_server.py"


def diagnostic(message="error", **kwargs):
    return {"range": copy.deepcopy(SPAN), "message": message, **kwargs}


class Peer:
    def __init__(self, path):
        self.report = {"uri": path.as_uri(), "source": "push", "version": 1, "items": []}
        self.closed = False
        self.before_reply = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def get_diagnostics(self, path, *, text):
        self.text = text
        if self.before_reply:
            self.before_reply()
        if isinstance(self.report, Exception):
            raise self.report
        return copy.deepcopy(self.report)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("x=1\n")
    tool = GetDiagnosticsTool(tmp_path, execution_allowed=True)
    peer = Peer(path)
    monkeypatch.setattr(tool, "_new_client", lambda _: peer)
    return tool, peer, path


@pytest.mark.parametrize(
    "source,version,verified", [("pull", 1, True), ("push", 1, True), ("push", None, False)]
)
def test_report_metadata_and_empty_reports(setup, source, version, verified):
    tool, peer, _ = setup
    peer.report.update(source=source, version=version)
    result = tool.execute({"path": "a.py"})
    assert result.success
    assert result.data["freshness_verified"] is verified
    assert result.data["total_diagnostics"] == 0
    assert peer.text == "x=1\n" and peer.closed


def test_dedup_counts_pagination_and_no_raw_data(setup):
    tool, peer, _ = setup
    error = diagnostic(
        severity=1, code="E", tags=[1], relatedInformation=[{}], data={"private": True}
    )
    warning = diagnostic("warning", severity=2)
    peer.report["items"] = [warning, error, copy.deepcopy(error), diagnostic("unknown")]
    result = tool.execute({"path": "a.py", "limit": 1})
    assert result.success
    assert result.data["total_diagnostics"] == 3
    assert result.data["severity_counts"] == {
        "error": 1,
        "warning": 1,
        "information": 0,
        "hint": 0,
        "unspecified": 1,
    }
    item = result.data["diagnostics"][0]
    assert item["range"]["start"] == {"line": 1, "column": 1}
    assert item["related_information_count"] == 1 and item["tags"] == ["unnecessary"]
    assert "data" not in item
    assert result.data["next_start_index"] == 1
    page = tool.execute({"path": "a.py", "start_index": 3})
    assert page.success and page.data["diagnostics"] == [] and page.data["total_diagnostics"] == 3
    assert tool.execute({"path": "a.py", "start_index": 4}).error_code == "INDEX_OUT_OF_RANGE"


def test_sort_ties_are_independent_of_server_order(setup):
    tool, peer, _ = setup
    peer.report["items"] = [
        diagnostic(code=1),
        diagnostic(code="1"),
        diagnostic(tags=[1]),
        diagnostic(tags=[2]),
    ]
    first = tool.execute({"path": "a.py"})
    peer.report["items"].reverse()
    second = tool.execute({"path": "a.py"})
    assert first.data["diagnostics"] == second.data["diagnostics"]


@pytest.mark.parametrize(
    "update",
    [
        {"uri": "file:///elsewhere.py"},
        {"source": "invalid"},
        {"version": True},
        {"version": -1},
        {"items": None},
        {"items": [diagnostic(severity=True)]},
        {"items": [diagnostic(code=True)]},
        {"items": [diagnostic(tags=[True])]},
        {"items": [{"message": "missing range"}]},
    ],
)
def test_reject_invalid_reports(setup, update):
    tool, peer, _ = setup
    peer.report.update(update)
    assert tool.execute({"path": "a.py"}).error_code == "LSP_INVALID_RESPONSE"


def test_timeout_is_not_empty_success(setup):
    tool, peer, _ = setup
    peer.report = LspTimeoutError("no report")
    result = tool.execute({"path": "a.py"})
    assert not result.success and result.error_code == "LSP_TIMEOUT"
    assert peer.closed


def test_file_change_and_limits(setup):
    tool, peer, path = setup
    tool.max_output_chars = 1
    assert tool.execute({"path": "a.py"}).error_code == "OUTPUT_TOO_LARGE"
    tool.max_output_chars = 20000
    peer.before_reply = lambda: path.write_text("changed")
    assert tool.execute({"path": "a.py"}).error_code == "FILE_CHANGED"


@pytest.mark.parametrize(
    "mode,expected_count,verified",
    [
        ("tool-diagnostics", 1, True),
        ("tool-unversioned", 1, False),
        ("tool-empty", 0, True),
        ("pull", 0, True),
    ],
)
def test_real_stdio_reports(tmp_path, mode, expected_count, verified):
    (tmp_path / "a.py").write_text("x=1\n")
    tool = GetDiagnosticsTool(
        tmp_path,
        execution_allowed=True,
        command=[sys.executable, str(SERVER), mode],
        timeout_seconds=2,
    )
    result = tool.execute({"path": "a.py"})
    assert result.success, result
    assert result.data["total_diagnostics"] == expected_count
    assert result.data["freshness_verified"] is verified


@pytest.mark.parametrize("mode", ["silent", "tool-stale-only"])
def test_real_stdio_missing_or_stale_reports_timeout(tmp_path, mode):
    (tmp_path / "a.py").write_text("x=1\n")
    tool = GetDiagnosticsTool(
        tmp_path,
        execution_allowed=True,
        command=[sys.executable, str(SERVER), mode],
        timeout_seconds=0.5,
    )
    assert tool.execute({"path": "a.py"}).error_code == "LSP_TIMEOUT"


def test_client_diagnoses_supplied_snapshot(tmp_path):
    from tools._internal.lsp_client import LspClient

    source = tmp_path / "a.py"
    source.write_text("on_disk")
    with LspClient(tmp_path, [sys.executable, str(SERVER), "pull"], language_id="python") as client:
        report = client.get_diagnostics(source, text="validated_snapshot")
        opened = client.request("test/documents")[source.as_uri()]
        assert opened["textDocument"]["text"] == "validated_snapshot"
        assert report["version"] == 1
    assert source.read_text() == "on_disk"


@pytest.mark.skipif(
    importlib.util.find_spec("pylsp") is None or importlib.util.find_spec("pyflakes") is None,
    reason="Requires the project's lsp extra, including pyflakes",
)
def test_real_pylsp_reports_python_syntax_error(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "broken.py").write_text("def broken(:\n    pass\n")
    result = GetDiagnosticsTool(project, execution_allowed=True).execute({"path": "broken.py"})
    assert result.success, result
    assert result.data["severity_counts"]["error"] >= 1
    assert any(item.get("source") == "pyflakes" for item in result.data["diagnostics"])
