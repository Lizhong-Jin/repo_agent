"""Trusted language inventory shared by installation, probes and LSP routing."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import scripts_dir


@dataclass(frozen=True)
class Language:
    name: str
    label: str
    toolchain_label: str
    service_label: str
    requirements: tuple[str, ...]
    formula: str | None = None


LANGUAGE_SPECS = (
    Language("python", "Python", "Python 3.11+", "pylsp", ()),
    Language(
        "typescript",
        "JavaScript / TypeScript / JSX / TSX",
        "Node.js + npm",
        "typescript-language-server + TypeScript",
        ("node", "npm"),
        "node",
    ),
    Language("go", "Go", "Go 1.25+", "gopls", ("go",), "go"),
    Language("cpp", "C / C++ / CUDA 文件", "LLVM / clangd", "clangd", ("clangd",), "llvm"),
)
LANGUAGES = tuple(item.name for item in LANGUAGE_SPECS)
LANGUAGE_BY_NAME = {item.name: item for item in LANGUAGE_SPECS}
TYPESCRIPT = "5.9.3"
TYPESCRIPT_SERVER = "4.3.4"
GOPLS = "v0.20.0"
GO_MINIMUM = (1, 25)


def tool_search_path(python, *, platform=None):
    selected = platform or sys.platform
    prefix = Path(python).absolute().parent.parent
    if selected == "linux":
        system = ["/usr/local/go/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    elif selected == "darwin":
        system = [
            "/opt/homebrew/opt/llvm/bin",
            "/usr/local/opt/llvm/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
    elif selected == "win32":
        prefix = Path(python).absolute().parent
        if prefix.name.lower() == "scripts":
            prefix = prefix.parent
        system = [str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")]
    else:
        raise ValueError("Toolchain search paths are not implemented on this platform")
    return os.pathsep.join(
        map(
            str, [scripts_dir(prefix, platform=selected), prefix / "lsp/node_modules/.bin", *system]
        )
    )


def sandbox_environment(python, scratch, *, platform):
    # Deliberately independent of the installer's network-enabled environment.
    return {
        "PATH": tool_search_path(python, platform=platform),
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "LANG": "C.UTF-8" if platform == "linux" else "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GOPATH": str(scratch / "go"),
        "GOCACHE": str(scratch / "go-build"),
        "GOMODCACHE": str(scratch / "go-mod"),
        "GOTOOLCHAIN": "local",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTELEMETRY": "off",
    }


def lsp_definitions(python):
    """Declarative rows; the tool layer constructs and validates its own registry."""
    typescript = ("typescript-language-server", "--stdio")
    clangd = ("clangd", "--background-index=false", "--clang-tidy=false", "-j=1")
    return (
        ("pylsp", "python", (".py", ".pyi"), (python, "-I", "-m", "pylsp"), 20),
        ("typescript-language-server", "javascript", (".js", ".mjs", ".cjs"), typescript, 20),
        ("typescript-language-server", "javascriptreact", (".jsx",), typescript, 20),
        ("typescript-language-server", "typescript", (".ts", ".mts", ".cts"), typescript, 20),
        ("typescript-language-server", "typescriptreact", (".tsx",), typescript, 20),
        ("gopls", "go", (".go",), ("gopls", "serve"), 30),
        ("clangd", "c", (".c", ".h"), clangd, 20),
        ("clangd", "cuda", (".cu", ".cuh"), clangd, 30),
        ("clangd", "cpp", (".cpp", ".cc", ".cxx", ".C", ".hpp", ".hh", ".hxx"), clangd, 20),
    )
