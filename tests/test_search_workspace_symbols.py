"""Workspace symbol boundaries, bounded preparation and native/fallback search."""

import copy
import importlib.util
import json
import shutil

import pytest

from tools import SearchWorkspaceSymbolsTool, create_default_tools
from tools._internal.lsp_client import LspResponseError, LspTimeoutError
from tools._internal.lsp_config import LspLanguageConfig, LspRegistry, default_lsp_registry

SPAN = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


def symbol(path, name="Example", **extra):
    return {"name": name, "kind": 12, "location": {"uri": path.as_uri(), "range": SPAN}, **extra}


def document(name="Example", **extra):
    return {"name": name, "kind": 12, "range": SPAN, "selectionRange": SPAN, **extra}


class Peer:
    def __init__(self):
        self.capabilities = {"workspaceSymbolProvider": True}
        self.result = []
        self.documents = [document()]
        self.calls = []
        self.opened = []
        self.entered = False
        self.closed = False
        self.resolve = lambda params: {**params, "location": {**params["location"], "range": SPAN}}
        self.on_document = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_):
        self.closed = True

    def sync_document(self, path, *, text, language_id):
        self.opened.append((path, text, language_id))
        return path.as_uri()

    def request(self, method, params, *, timeout):
        assert timeout > 0
        self.calls.append((method, copy.deepcopy(params), timeout))
        if method == "textDocument/documentSymbol":
            if self.on_document:
                self.on_document()
            result = self.documents
        elif method == "workspaceSymbol/resolve":
            result = self.resolve(params)
        else:
            assert method == "workspace/symbol"
            result = self.result
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    peer = Peer()

    def make(**kwargs):
        kwargs.setdefault("execution_allowed", True)
        tool = SearchWorkspaceSymbolsTool(root, **kwargs)
        monkeypatch.setattr(tool, "_new_client", lambda _: peer)
        return tool

    return root, peer, make


def query(tool, **kwargs):
    return tool.execute({"query": "Example", "server_id": "pylsp", **kwargs})


def test_execution_denied_before_client_creation(setup):
    _, peer, make = setup
    result = query(make(execution_allowed=False))
    assert result.error_code == "PERMISSION_DENIED"
    assert not peer.entered


def test_factory_registration_and_execution_boundary(tmp_path):
    for isolated in [False, True]:
        tools = create_default_tools(tmp_path, isolated_execution=isolated)
        matches = [t for t in tools if t.definition.name == "search_workspace_symbols"]
        assert len(matches) == int(isolated)
        if matches:
            assert matches[0].execution_allowed


@pytest.mark.parametrize("allow_external", [False, True])
def test_path_resolution_and_protected_counts(setup, allow_external):
    root, peer, make = setup
    external = root.parent / "outside.py"
    external.write_text("x = 1\n")
    (root / "alias.py").symlink_to(external)
    (root / ".env").write_text("dummy")
    (root / "public.py").symlink_to(root / ".env")
    (root / ".env.py").symlink_to(root / "a.py")
    protected_external = root.parent / ".ssh" / "id_rsa"
    peer.result = [
        symbol(root / "a.py"),
        symbol(root / ".." / "outside.py"),
        symbol(root / "alias.py"),
        symbol(root / "public.py"),
        symbol(root / ".env.py"),
        symbol(protected_external),
    ]
    result = query(make(allow_external_locations=allow_external))
    assert result.success
    assert result.data["omitted_protected_symbols"] == 3
    assert result.data["omitted_external_symbols"] == (0 if allow_external else 2)
    assert result.data["symbols"][0]["path"] == "a.py"
    assert all(".." not in s["path"] for s in result.data["symbols"])
    if allow_external:
        assert all(not s["in_workspace"] for s in result.data["symbols"][1:])


def test_unique_servers_group_languages_and_use_largest_timeout(setup):
    _, _, make = setup
    configs = tuple(c for c in default_lsp_registry().languages if c.server_id == "clangd")
    tool = make(lsp_registry=LspRegistry(configs))
    assert tool.definition.parameters["properties"]["server_id"]["enum"] == ["clangd"]
    selected = tool._select_workspace_symbol_server(None)
    assert selected.timeout_seconds == 30
    result = tool.execute({"query": "Example"})
    assert result.success
    assert result.data["language_ids"] == ["c", "cuda", "cpp"]
    assert "language_id" not in result.data


def test_conflicting_server_commands_are_rejected(setup):
    _, _, make = setup
    configs = (
        LspLanguageConfig("same", "a", (".a",), ("first",)),
        LspLanguageConfig("same", "b", (".b",), ("second",)),
    )
    with pytest.raises(ValueError, match="Conflicting commands"):
        make(lsp_registry=LspRegistry(configs))


def test_empty_registry_and_missing_server_are_actionable(setup):
    _, peer, make = setup
    assert (
        make(lsp_registry=LspRegistry(())).execute({"query": "x"}).error_code == "LSP_UNAVAILABLE"
    )
    result = make().execute({"query": "x"})
    assert result.error_code == "INVALID_ARGUMENTS"
    assert not peer.entered
    properties = make(lsp_registry=LspRegistry(())).definition.parameters["properties"]
    assert "enum" not in properties["server_id"]


def test_prepare_before_native_query_with_each_documents_language(setup):
    root, peer, make = setup
    (root / "a.cpp").write_text("int Example();")
    (root / "a.cu").write_text("int Example();")
    peer.capabilities["documentSymbolProvider"] = {}
    peer.result = [symbol(root / "a.cpp")]
    result = query(make(), server_id="clangd")
    assert result.success
    assert [c[0] for c in peer.calls] == [
        "textDocument/documentSymbol",
        "textDocument/documentSymbol",
        "workspace/symbol",
    ]
    assert {entry[2] for entry in peer.opened} == {"cpp", "cuda"}
    assert result.data["coverage"]["scanned_files"] == 2
    assert result.data["coverage"]["index_completeness"] == "unknown"
    assert peer.closed


def test_document_fallback_flattens_hierarchy_and_filters_names(setup):
    root, peer, make = setup
    peer.capabilities = {"documentSymbolProvider": True}
    peer.documents = [document("Parent", children=[document("myEXAMPLE")]), document("Other")]
    result = query(make())
    assert result.success and result.data["search_mode"] == "document_symbols"
    assert result.data["symbols"][0]["name"] == "myEXAMPLE"
    assert result.data["symbols"][0]["container_name"] == "Parent"
    assert result.data["symbols"][0]["path"] == "a.py"
    assert result.data["total_symbols"] == 1
    assert all(c[0] != "workspace/symbol" for c in peer.calls)
    peer.documents = [symbol(root / "a.py")]
    assert query(make()).success


@pytest.mark.parametrize(
    "option", ["max_workspace_files", "max_workspace_entries", "max_workspace_bytes"]
)
def test_scan_limits_are_visible(setup, option):
    root, peer, make = setup
    (root / "b.py").write_text("x = 2\n")
    peer.capabilities = {"documentSymbolProvider": True}
    result = query(make(**{option: 1}))
    assert result.success
    assert result.data["coverage"]["scan_truncated"]
    assert result.data["coverage"]["scanned_files"] <= 1


def test_scan_skips_generated_protected_external_and_unreadable_sources(setup):
    root, peer, make = setup
    peer.capabilities = {"documentSymbolProvider": True}
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dependency.py").write_text("x")
    (root / ".env.py").write_text("dummy")
    (root / "binary.py").write_bytes(b"\0")
    external = root.parent / "outside.py"
    external.write_text("x")
    (root / "alias.py").symlink_to(external)
    result = query(make())
    assert result.success
    assert [entry[0].name for entry in peer.opened] == ["a.py"]
    assert result.data["coverage"]["skipped_files"] == 2


def test_changed_snapshot_does_not_enter_document_fallback_results(setup):
    root, peer, make = setup
    peer.capabilities = {"documentSymbolProvider": True}
    peer.on_document = lambda: (root / "a.py").write_text("changed")
    result = query(make())
    assert result.success and result.data["symbols"] == []
    assert result.data["coverage"]["skipped_files"] == 1


def test_resolve_retains_opaque_data_and_rechecks_protected_path(setup):
    root, peer, make = setup
    peer.capabilities = {"workspaceSymbolProvider": {"resolveProvider": True}}
    raw = symbol(root / "a.py", location={"uri": (root / "a.py").as_uri()}, data={"key": 42})
    peer.result = [raw]
    result = query(make())
    assert result.success and result.data["unresolved_locations"] == 0
    assert peer.calls[-1][0:2] == ("workspaceSymbol/resolve", raw)
    assert "data" not in result.data["symbols"][0]
    peer.resolve = lambda params: symbol(root / ".env")
    result = query(make())
    assert result.success and result.data["omitted_protected_symbols"] == 1
    assert not result.data["symbols"]


def test_resolve_failure_keeps_explicit_unresolved_result(setup):
    root, peer, make = setup
    peer.capabilities = {"workspaceSymbolProvider": {"resolveProvider": True}}
    peer.result = [symbol(root / "a.py", location={"uri": (root / "a.py").as_uri()})]
    peer.resolve = lambda _: LspResponseError({"code": -32601, "message": "unsupported"})
    result = query(make())
    assert result.success and result.data["unresolved_locations"] == 1
    assert not result.data["symbols"][0]["location_resolved"]


def test_resolve_only_visible_results_and_skip_protected(setup):
    root, peer, make = setup
    peer.capabilities = {"workspaceSymbolProvider": {"resolveProvider": True}}
    peer.result = [
        symbol(path, name=str(index), location={"uri": path.as_uri()})
        for index, path in enumerate([root / ".env", root / "a.py", root / "b.py"])
    ]
    result = query(make(), limit=1)
    assert result.success
    assert sum(c[0] == "workspaceSymbol/resolve" for c in peer.calls) == 1


def test_order_deduplication_and_limit(setup):
    root, peer, make = setup
    peer.result = [
        symbol(root / "a.py", "Z"),
        symbol(root / "a.py", "A"),
        symbol(root / "a.py", "Z"),
    ]
    result = query(make(), limit=1)
    assert result.success
    assert result.data["symbols"][0]["name"] == "Z"
    assert result.data["total_symbols"] == 2 and result.data["truncated"]


def test_output_budget_reduces_count_and_omits_oversized_identifiers(setup):
    root, peer, make = setup
    peer.result = [symbol(root / "a.py", "x" * 30_000)] + [
        symbol(root / "a.py", str(index) + "\n" * 40) for index in range(100)
    ]
    result = query(make(max_output_chars=1_000))
    assert result.success
    assert len(json.dumps(result.data, ensure_ascii=False)) <= 1_000
    assert result.data["omitted_oversized_symbols"] == 1
    assert 0 < result.data["returned_symbols"] < 100
    assert result.data["truncated"]
    assert query(make(max_output_chars=1)).error_code == "OUTPUT_TOO_LARGE"


@pytest.mark.parametrize("payload", [{}, [None], [{"name": "Example"}]])
def test_invalid_native_results(setup, payload):
    _, peer, make = setup
    peer.result = payload
    assert query(make()).error_code == "LSP_INVALID_RESPONSE"


@pytest.mark.parametrize("payload", [None, []])
def test_empty_native_results(setup, payload):
    _, peer, make = setup
    peer.result = payload
    assert query(make()).data["total_symbols"] == 0


@pytest.mark.parametrize("payload", [[None], {}, [document(children=None)]])
def test_invalid_fallback_results(setup, payload):
    _, peer, make = setup
    peer.capabilities = {"documentSymbolProvider": True}
    peer.documents = payload
    assert query(make()).error_code == "LSP_INVALID_RESPONSE"


def test_timeout_is_bounded_and_client_is_closed(setup):
    _, peer, make = setup
    peer.result = LspTimeoutError("timeout")
    result = query(make(timeout_seconds=0.5))
    assert result.error_code == "LSP_TIMEOUT"
    assert peer.closed and peer.calls[0][2] <= 0.5


def test_neither_capability_returns_unsupported(setup):
    _, peer, make = setup
    peer.capabilities = {}
    assert query(make()).error_code == "LSP_UNSUPPORTED"


@pytest.mark.parametrize("server", ["pylsp", "clangd"])
def test_real_unopened_source_search(tmp_path, monkeypatch, server):
    if server == "pylsp" and importlib.util.find_spec("pylsp") is None:
        pytest.skip("pylsp not installed")
    if server == "clangd" and shutil.which("clangd") is None:
        pytest.skip("clangd not installed")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("def UniqueWorkspaceSymbol():\n    return 42\n")
    path = root / "a.cpp"
    path.write_text("int UniqueWorkspaceSymbol() { return 42; }\n")
    (root / "compile_commands.json").write_text(
        json.dumps(
            [{"directory": str(root), "file": str(path), "arguments": ["clang++", "-c", str(path)]}]
        )
    )
    tool = SearchWorkspaceSymbolsTool(root, execution_allowed=True, timeout_seconds=15)
    result = tool.execute({"query": "UniqueWorkspaceSymbol", "server_id": server})
    assert result.success, result
    assert any(s["name"] == "UniqueWorkspaceSymbol" for s in result.data["symbols"])
    assert result.data["coverage"]["scanned_files"] == 1
    assert result.data["search_mode"] == ("document_symbols" if server == "pylsp" else "workspace")


def test_resolved_duplicates_collapse_to_one_result(setup):
    root, peer, make = setup
    peer.capabilities = {"workspaceSymbolProvider": {"resolveProvider": True}}
    raw = symbol(root / "a.py", location={"uri": (root / "a.py").as_uri()}, data={"key": 42})
    peer.result = [raw, copy.deepcopy(raw)]
    result = query(make(), limit=1)
    assert result.success and result.data["total_symbols"] == 1
    assert result.data["unresolved_locations"] == 0
    assert sum(c[0] == "workspaceSymbol/resolve" for c in peer.calls) == 1


def test_exact_output_budget_preserves_all_symbols(setup):
    root, peer, make = setup
    peer.result = [symbol(root / "a.py")]
    original = query(make())
    budget = len(json.dumps(original.data, ensure_ascii=False))
    result = query(make(max_output_chars=budget))
    assert result.success and result.data == original.data


def test_single_oversized_symbol_returns_explicit_omission(setup):
    root, peer, make = setup
    peer.result = [symbol(root / "a.py", "x" * 30_000)]
    result = query(make(), limit=1)
    assert result.success and result.data["symbols"] == []
    assert result.data["omitted_oversized_symbols"] == 1
    assert result.data["truncated"]
