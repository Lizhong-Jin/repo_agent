"""References tool contracts and shared definition/reference path protection."""

import copy
import importlib.util
import logging

import pytest

from tools import FindReferencesTool, GoToDefinitionsTool, create_default_tools
from tools._internal.lsp_client import LspResponseError, LspTimeoutError, LspUnsupportedError

SPAN = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


def location(path):
    return {"uri": path.as_uri(), "range": copy.deepcopy(SPAN)}


class Peer:
    capabilities = {"referencesProvider": {}, "definitionProvider": True}
    stderr = ""

    def __init__(self, result):
        self.result = result
        self.closed = False
        self.before_reply = None
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def sync_document(self, path, *, text):
        self.text = text
        return path.as_uri()

    def request(self, method, params):
        self.method, self.params = method, params
        if self.before_reply:
            self.before_reply()
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.py").write_text("x = 2\n", encoding="utf-8")
    tool = FindReferencesTool(root, execution_allowed=True)
    peer = Peer([location(root / "b.py"), location(root / "a.py"), location(root / "a.py")])
    monkeypatch.setattr(tool, "_new_client", lambda _: peer)
    return tool, peer, {"path": "a.py", "line": 1, "column": 1}


@pytest.mark.parametrize("include", [True, False])
def test_request_sort_deduplicate_and_paginate(setup, include):
    tool, peer, args = setup
    result = tool.execute({**args, "limit": 1, "include_declaration": include})
    assert result.success
    assert peer.method == "textDocument/references"
    assert peer.params["position"] == {"line": 0, "character": 0}
    assert peer.params["context"] == {"includeDeclaration": include}
    assert result.data["total_references"] == 2
    assert result.data["references"][0]["path"] == "a.py"
    assert result.data["next_start_index"] == 1
    peer.result.reverse()
    page = tool.execute({**args, "start_index": 1})
    assert page.data["references"][0]["path"] == "b.py"
    assert page.data["references"][0]["index"] == 1
    assert page.data["next_start_index"] is None
    assert peer.closed


@pytest.mark.parametrize("payload", [None, []])
def test_explicit_empty_results(setup, payload):
    tool, peer, args = setup
    peer.result = payload
    result = tool.execute(args)
    assert result.success and result.data["total_references"] == 0


@pytest.mark.parametrize("kind", ["single", "link", "mixed", "missing-range", "invalid-range"])
def test_reject_non_reference_response_shapes(setup, kind):
    tool, peer, args = setup
    loc = location(tool.workspace_root / "a.py")
    link = {"targetUri": loc["uri"], "targetRange": SPAN, "targetSelectionRange": SPAN}
    peer.result = {
        "single": loc,
        "link": [link],
        "mixed": [loc, link],
        "missing-range": [{"uri": loc["uri"]}],
        "invalid-range": [{"uri": loc["uri"], "range": {}}],
    }[kind]
    assert tool.execute(args).error_code == "LSP_INVALID_RESPONSE"


@pytest.mark.parametrize("tool_type", [FindReferencesTool, GoToDefinitionsTool])
@pytest.mark.parametrize("allow_external", [False, True])
def test_protected_aliases_and_external_locations(tmp_path, monkeypatch, tool_type, allow_external):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "a.py"
    source.write_text("x = 1\n")
    external = tmp_path / "library.py"
    external.write_text("x = 1\n")
    protected_external = tmp_path / ".ssh" / "id_rsa"
    protected_external.parent.mkdir()
    protected_external.write_text("not-a-real-key")
    alias = root / ".env.py"
    alias.symlink_to(source)
    secret = root / ".env"
    secret.write_text("test")
    public_alias = root / "alias.py"
    public_alias.symlink_to(secret)
    tool = tool_type(root, execution_allowed=True, allow_external_locations=allow_external)
    peer = Peer([location(p) for p in (source, external, protected_external, alias, public_alias)])
    monkeypatch.setattr(tool, "_new_client", lambda _: peer)
    result = tool.execute({"path": "a.py", "line": 1, "column": 1})
    assert result.success
    field = "references" if tool_type is FindReferencesTool else "definitions"
    assert len(result.data[field]) == (2 if allow_external else 1)
    assert result.data[f"omitted_external_{field}"] == (3 if allow_external else 4)
    assert all(".env" not in str(item) and "id_rsa" not in str(item) for item in result.data[field])


@pytest.mark.parametrize(
    "uri", ["file:a.py", "file:///a.py%00", "file:///a.py?x=1", "file:///a.py#x"]
)
def test_reject_malformed_file_uris(setup, uri):
    tool, peer, args = setup
    peer.result = [{"uri": uri, "range": SPAN}]
    assert tool.execute(args).error_code == "LSP_INVALID_RESPONSE"


def test_definition_links_still_work(tmp_path, monkeypatch):
    path = tmp_path / "a.py"
    path.write_text("x=1")
    tool = GoToDefinitionsTool(tmp_path, execution_allowed=True)
    peer = Peer([{"targetUri": path.as_uri(), "targetRange": SPAN, "targetSelectionRange": SPAN}])
    monkeypatch.setattr(tool, "_new_client", lambda _: peer)
    result = tool.execute({"path": "a.py", "line": 1, "column": 1})
    assert result.success and result.data["definitions"][0]["path"] == "a.py"


@pytest.mark.parametrize(
    "override",
    [
        {"line": 99},
        {"column": 99},
        {"include_declaration": 1},
        {"limit": True},
        {"start_index": -1},
    ],
)
def test_invalid_arguments_never_query_server(setup, override):
    tool, peer, args = setup
    assert tool.execute({**args, **override}).error_code == "INVALID_ARGUMENTS"
    assert peer.params is None


def test_utf16_position(setup):
    tool, peer, args = setup
    (tool.workspace_root / "a.py").write_text('s="😀"; x=1\r\nx\n', encoding="utf-8")
    assert tool.execute({**args, "column": 9}).success
    assert peer.params["position"] == {"line": 0, "character": 8}
    assert tool.execute({**args, "line": 2}).success
    assert peer.params["position"] == {"line": 1, "character": 0}


def test_changed_file_and_output_limit(setup):
    tool, peer, args = setup
    tool.max_output_chars = 1
    assert tool.execute(args).error_code == "OUTPUT_TOO_LARGE"
    tool.max_output_chars = 20000
    peer.before_reply = lambda: (tool.workspace_root / "a.py").write_text("changed\n")
    assert tool.execute(args).error_code == "FILE_CHANGED"


@pytest.mark.parametrize(
    "error, code",
    [
        (LspTimeoutError("timeout"), "LSP_TIMEOUT"),
        (LspUnsupportedError("unsupported"), "LSP_UNSUPPORTED"),
        (LspResponseError({"code": -1, "message": "failure"}), "LSP_REQUEST_FAILED"),
    ],
)
def test_errors_and_cleanup(setup, error, code):
    tool, peer, args = setup
    peer.result = error
    assert tool.execute(args).error_code == code
    assert peer.closed


def test_stderr_is_bounded_debug_log_not_model_output(setup, caplog):
    tool, peer, args = setup
    peer.stderr = "hidden-prefix" + "x" * 9000
    with caplog.at_level(logging.DEBUG, logger="tools.semantic"):
        result = tool.execute(args)
    assert result.success
    assert "LSP server pylsp stderr:" in caplog.text
    assert "hidden-prefix" not in caplog.text
    assert "stderr" not in result.data
    assert len(caplog.records[-1].args[-1]) == 8192


def test_registration(tmp_path):
    for enabled in (True, False):
        names = {
            t.definition.name for t in create_default_tools(tmp_path, isolated_execution=enabled)
        }
        assert ("find_references" in names) is enabled
        assert ("get_diagnostics" in names) is enabled


@pytest.mark.skipif(importlib.util.find_spec("pylsp") is None, reason="Requires pylsp")
def test_real_pylsp_cross_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("def target():\n    return 1\n\ntarget()\n")
    (root / "b.py").write_text("from a import target\n\ntarget()\n")
    tool = FindReferencesTool(root, execution_allowed=True)
    args = {"path": "a.py", "line": 1, "column": 6}
    for include in (True, False):
        result = tool.execute({**args, "include_declaration": include})
        assert result.success, result
        locations = {(x["path"], x["range"]["start"]["line"]) for x in result.data["references"]}
        assert ("a.py", 4) in locations and ("b.py", 3) in locations
        assert (("a.py", 1) in locations) is include
