from dataclasses import FrozenInstanceError

import pytest

from tools.lsp_config import LspLanguageConfig, LspRegistry, default_lsp_registry


@pytest.mark.parametrize(
    "filename, language, server",
    [
        ("main.py", "python", "pylsp"),
        ("main.pyi", "python", "pylsp"),
        ("main.PY", "python", "pylsp"),
        ("index.js", "javascript", "typescript-language-server"),
        ("index.mjs", "javascript", "typescript-language-server"),
        ("index.cjs", "javascript", "typescript-language-server"),
        ("View.jsx", "javascriptreact", "typescript-language-server"),
        ("index.ts", "typescript", "typescript-language-server"),
        ("index.d.ts", "typescript", "typescript-language-server"),
        ("index.mts", "typescript", "typescript-language-server"),
        ("index.cts", "typescript", "typescript-language-server"),
        ("View.tsx", "typescriptreact", "typescript-language-server"),
        ("main.go", "go", "gopls"),
        ("main.c", "c", "clangd"),
        ("main.h", "c", "clangd"),
        ("main.C", "cpp", "clangd"),
        ("main.cpp", "cpp", "clangd"),
        ("main.cc", "cpp", "clangd"),
        ("main.cxx", "cpp", "clangd"),
        ("main.hpp", "cpp", "clangd"),
        ("main.hh", "cpp", "clangd"),
        ("main.hxx", "cpp", "clangd"),
    ],
)
def test_default_routes(filename, language, server):
    config = default_lsp_registry().select("directory.ts/" + filename)
    assert config.language_id == language
    assert config.server_id == server


@pytest.mark.parametrize(
    "filename", ["README", "data.json", "index.vue", "module.rs", "test.py.bak"]
)
def test_unconfigured_languages(filename):
    assert default_lsp_registry().select(filename) is None


def test_longest_suffix_wins_independent_of_order():
    base = LspLanguageConfig("base", "typescript", (".ts",), ("server",))
    specific = LspLanguageConfig("declarations", "typescript", (".d.ts",), ("other-server",))
    for registry in (LspRegistry((base, specific)), LspRegistry((specific, base))):
        assert registry.select("types.d.ts") is specific
        assert registry.select("types.D.TS") is specific
        assert registry.select("main.ts") is base


def test_registry_copies_inputs_and_rejects_duplicate_suffixes():
    command = ["server", "--stdio"]
    extensions = [".custom"]
    config = LspLanguageConfig("custom", "custom", extensions, command)
    configs = [config]
    registry = LspRegistry(configs)
    configs.clear()
    command.clear()
    extensions.clear()
    assert registry.select("example.custom").command == ("server", "--stdio")
    with pytest.raises(FrozenInstanceError):
        config.language_id = "changed"
    with pytest.raises(ValueError, match="Duplicate"):
        LspRegistry((config, config))
    assert LspRegistry(()).select("a.py") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"command": "server"},
        {"command": []},
        {"command": [""]},
        {"command": ["a\0b"]},
        {"language_id": ""},
        {"server_id": ""},
        {"file_extensions": ".py"},
        {"file_extensions": ("py",)},
        {"file_extensions": (".foo/bar",)},
        {"file_extensions": (".",)},
        {"timeout_seconds": True},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 0},
    ],
)
def test_invalid_config(changes):
    values = {
        "server_id": "server",
        "language_id": "language",
        "file_extensions": (".source",),
        "command": ("server",),
    }
    values.update(changes)
    with pytest.raises(ValueError):
        LspLanguageConfig(**values)
