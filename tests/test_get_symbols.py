import copy
import sys
from pathlib import Path

import pytest

from tools import GetSymbolsTool, create_default_tools
from tools.lsp_client import LspError, LspResponseError, LspTimeoutError, LspUnsupportedError

RANGE = {"start": {"line": 1, "character": 2}, "end": {"line": 3, "character": 4}}


def symbol(name="Example", kind=5, **extra):
    return {
        "name": name,
        "kind": kind,
        "range": copy.deepcopy(RANGE),
        "selectionRange": copy.deepcopy(RANGE),
        **extra,
    }


class StubClient:
    capabilities = {"documentSymbolProvider": {}}

    def __init__(self, symbols):
        self.symbols = symbols
        self.closed = False
        self.text = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def sync_document(self, path, *, text):
        self.text = text
        return path.as_uri()

    def request(self, method, params):
        assert method == "textDocument/documentSymbol"
        if isinstance(self.symbols, Exception):
            raise self.symbols
        return self.symbols


@pytest.fixture
def tool(tmp_path, monkeypatch):
    (tmp_path / "example.py").write_text("class Example:\n    pass\n", encoding="utf-8")
    instance = GetSymbolsTool(tmp_path, execution_allowed=True)
    peer = StubClient([symbol(children=[symbol("method", 6)]), symbol("func", 12)])
    monkeypatch.setattr(instance, "_new_client", lambda config: peer)
    return instance, peer


def test_hierarchy_pagination_and_positions(tool):
    instance, peer = tool
    result = instance.execute({"path": "example.py", "limit": 1})
    assert result.success
    assert result.data["total_symbols"] == 3
    assert result.data["hierarchical"]
    assert result.data["next_start_index"] == 1
    assert result.data["symbols"][0]["range"] == {
        "start": {"line": 2, "column": 3},
        "end": {"line": 4, "column": 5},
    }
    result = instance.execute({"path": "example.py", "start_index": 1})
    assert result.success
    assert result.data["symbols"][0]["parent_index"] == 0
    assert result.data["symbols"][1]["parent_index"] is None
    assert not result.data["truncated"]
    assert peer.closed
    assert peer.text == "class Example:\n    pass\n"


def test_flat_symbols_do_not_invent_hierarchy(tool):
    instance, peer = tool
    peer.symbols = [
        {
            "name": "method",
            "kind": 6,
            "containerName": "Example",
            "location": {
                "uri": (instance.workspace_root / "example.py").as_uri(),
                "range": RANGE,
            },
        }
    ]
    result = instance.execute({"path": "example.py"})
    assert result.success
    assert not result.data["hierarchical"]
    assert "parent_index" not in result.data["symbols"][0]
    assert result.data["symbols"][0]["container_name"] == "Example"
    # pylsp sends containerName=null for top-level symbols.
    peer.symbols[0]["containerName"] = None
    result = instance.execute({"path": "example.py"})
    assert result.success
    assert "container_name" not in result.data["symbols"][0]


@pytest.mark.parametrize("symbols", [None, []])
def test_empty_symbols(tool, symbols):
    instance, peer = tool
    peer.symbols = symbols
    result = instance.execute({"path": "example.py"})
    assert result.success and result.data["symbols"] == []
    assert result.data["next_start_index"] is None


@pytest.mark.parametrize(
    "symbols",
    [[None], {}, [symbol(kind=True)], [symbol(children={})], [symbol(range={})], [symbol(name="")]],
)
def test_malformed_response(tool, symbols):
    instance, peer = tool
    peer.symbols = symbols
    assert instance.execute({"path": "example.py"}).error_code == "LSP_INVALID_RESPONSE"
    assert peer.closed


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"path": ""},
        {"path": "example.py", "limit": True},
        {"path": "example.py", "start_index": -1},
        {"path": "example.py", "limit": 201},
        {"path": "example.py", "command": ["anything"]},
    ],
)
def test_invalid_arguments(tool, arguments):
    instance, peer = tool
    assert instance.execute(arguments).error_code == "INVALID_ARGUMENTS"
    assert peer.text is None


@pytest.mark.parametrize(
    "path, code",
    [
        ("../outside.py", "PATH_OUTSIDE_WORKSPACE"),
        (".env", "PROTECTED_FILE"),
        ("logs/a.py", "PROTECTED_FILE"),
        ("missing.py", "FILE_NOT_FOUND"),
        (".", "NOT_A_FILE"),
        ("example.txt", "UNSUPPORTED_LANGUAGE"),
    ],
)
def test_paths_rejected_before_server(tool, path, code):
    instance, peer = tool
    (instance.workspace_root / "example.txt").write_text("x")
    assert instance.execute({"path": path}).error_code == code
    assert peer.text is None


def test_symlink_protection(tool):
    instance, peer = tool
    (instance.workspace_root / ".env").write_text("secret")
    (instance.workspace_root / "alias.py").symlink_to(".env")
    assert instance.execute({"path": "alias.py"}).error_code == "PROTECTED_FILE"
    assert peer.text is None


@pytest.mark.parametrize("raw, code", [(b"x\0", "BINARY_FILE"), (b"\xff", "UNSUPPORTED_ENCODING")])
def test_invalid_content(tool, raw, code):
    instance, peer = tool
    (instance.workspace_root / "example.py").write_bytes(raw)
    assert instance.execute({"path": "example.py"}).error_code == code
    assert peer.text is None


def test_file_size_limit_rejected_before_server(tool):
    instance, peer = tool
    instance.max_file_bytes = 1
    result = instance.execute({"path": "example.py"})
    assert not result.success
    assert result.error_code == "FILE_TOO_LARGE"
    assert peer.text is None


def test_output_size_limit(tool):
    instance, _ = tool
    instance.max_output_chars = 1
    assert instance.execute({"path": "example.py"}).error_code == "OUTPUT_TOO_LARGE"


def test_changed_file_rejects_stale_symbols(tool, monkeypatch):
    instance, peer = tool
    original = peer.request

    def change(*args):
        (instance.workspace_root / "example.py").write_text("changed")
        return original(*args)

    monkeypatch.setattr(peer, "request", change)
    result = instance.execute({"path": "example.py"})
    assert not result.success
    assert result.error_code == "FILE_CHANGED"
    assert result.data == {}
    assert peer.closed


@pytest.mark.parametrize(
    "error, code",
    [
        (LspTimeoutError("private"), "LSP_TIMEOUT"),
        (LspUnsupportedError("private"), "LSP_UNSUPPORTED"),
        (LspResponseError({"message": "private", "code": -1}), "LSP_REQUEST_FAILED"),
        (LspError("private"), "LSP_ERROR"),
        (FileNotFoundError("private"), "LSP_UNAVAILABLE"),
    ],
)
def test_errors_and_cleanup(tool, error, code):
    instance, peer = tool
    peer.symbols = error
    result = instance.execute({"path": "example.py"})
    assert result.error_code == code
    assert "private" not in str(result)
    assert peer.closed


def test_no_host_execution_and_factory_registration(tmp_path):
    (tmp_path / "example.py").write_text("x")
    assert (
        GetSymbolsTool(tmp_path).execute({"path": "example.py"}).error_code == "PERMISSION_DENIED"
    )
    assert "get_symbols" not in {t.definition.name for t in create_default_tools(tmp_path)}
    assert "get_symbols" in {
        t.definition.name for t in create_default_tools(tmp_path, isolated_execution=True)
    }


def test_real_stdio_transport(tmp_path):
    # The deterministic peer uses the same framing and lifecycle as an external server.
    server = Path(__file__).parent / "fixtures" / "lsp_server.py"
    (tmp_path / "example.py").write_text("def example():\n    pass\n")
    instance = GetSymbolsTool(
        tmp_path, execution_allowed=True, command=[sys.executable, str(server), "tool-symbols"]
    )
    result = instance.execute({"path": "example.py"})
    assert result.success, result
    assert result.data["symbols"][0]["name"] == "example"


def test_mixed_languages_route_per_call_without_state_leaks(tmp_path, monkeypatch):
    from tools.lsp_config import default_lsp_registry

    instance = GetSymbolsTool(tmp_path, execution_allowed=True)
    selected = []

    def new_client(config):
        selected.append(config)
        return StubClient([symbol()])

    monkeypatch.setattr(instance, "_new_client", new_client)
    for filename in ("example.py", "view.tsx", "main.go", "main.C", "example.py"):
        (tmp_path / filename).write_text("source")
        result = instance.execute({"path": filename})
        expected = default_lsp_registry().select(filename)
        assert result.success
        assert result.data["language_id"] == expected.language_id
        assert result.data["server_id"] == expected.server_id
        assert selected[-1] == expected
    assert len(selected) == 5


def test_custom_registry_factory_and_legacy_arguments(tmp_path):
    from tools.lsp_config import LspLanguageConfig, LspRegistry

    custom = LspLanguageConfig("custom", "rust", (".rs",), ("rust-analyzer",), 42)
    registry = LspRegistry((custom,))
    tools = create_default_tools(tmp_path, isolated_execution=True, lsp_registry=registry)
    instance = next(t for t in tools if t.definition.name == "get_symbols")
    assert instance.lsp_registry.select("main.rs") is custom
    assert instance.lsp_registry.select("main.py") is None
    assert "rust (.rs)" in instance.definition.description
    assert "Python" not in instance.definition.description
    client = instance._new_client(custom)
    assert client.language_id == "rust"
    assert client.command == ["rust-analyzer"]
    assert client.timeout_seconds == 42
    override = GetSymbolsTool(tmp_path, lsp_registry=registry, timeout_seconds=3)
    assert override._new_client(custom).timeout_seconds == 3
    legacy = GetSymbolsTool(tmp_path, command=["legacy-server"])
    config = legacy.lsp_registry.select("main.py")
    assert config.command == ("legacy-server",)
    assert legacy.lsp_registry.select("main.ts") is None
    with pytest.raises(ValueError, match="either"):
        GetSymbolsTool(tmp_path, lsp_registry=registry, command=["server"])


def test_symlink_routes_by_validated_target(tmp_path, monkeypatch):
    (tmp_path / "source.ts").write_text("export const value = 1;")
    (tmp_path / "alias.py").symlink_to("source.ts")
    instance = GetSymbolsTool(tmp_path, execution_allowed=True)
    selected = []

    def new_client(config):
        selected.append(config.language_id)
        return StubClient([])

    monkeypatch.setattr(instance, "_new_client", new_client)
    assert instance.execute({"path": "alias.py"}).success
    assert selected == ["typescript"]


def test_missing_language_server_is_explicit_error(tmp_path):
    from tools.lsp_config import LspLanguageConfig, LspRegistry

    (tmp_path / "example.ts").write_text("export const x = 1;")
    registry = LspRegistry(
        (
            LspLanguageConfig(
                "missing-server",
                "typescript",
                (".ts",),
                (str(tmp_path / "missing"),),
            ),
        )
    )
    result = GetSymbolsTool(tmp_path, lsp_registry=registry, execution_allowed=True).execute(
        {"path": "example.ts"}
    )
    assert result.error_code == "LSP_UNAVAILABLE"
    assert "missing-server" in result.error
