"""Trusted, immutable language-server configuration and file routing.

Configuration is application-owned; never load executable commands from model
arguments or an untrusted repository. Construct a new registry to add/override
languages. Runtime dependencies for defaults live in sandbox/Dockerfile.
"""

import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LspLanguageConfig:
    server_id: str
    language_id: str
    file_extensions: tuple[str, ...]
    command: tuple[str, ...]
    timeout_seconds: float = 20

    def __post_init__(self) -> None:
        for name in ("server_id", "language_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\0" in value:
                raise ValueError(f"{name} must be a non-empty string without NUL")
        for name in ("command", "file_extensions"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
                raise ValueError(f"{name} must be a non-empty sequence")
            if any(not isinstance(x, str) or "\0" in x for x in value):
                raise ValueError(f"{name} must contain strings without NUL")
            object.__setattr__(self, name, tuple(value))
        if not self.command[0].strip():
            raise ValueError("command requires an executable")
        if any(
            not suffix.startswith(".")
            or len(suffix) < 2
            or any(char in suffix for char in "/\\ \t\n\r")
            for suffix in self.file_extensions
        ):
            raise ValueError("file_extensions must contain suffixes such as .py or .d.ts")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (float, int))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class LspRegistry:
    """Longest suffix wins, with exact case preferred over lowercase fallback.

    This preserves the C/C++ distinction between .c and .C. Duplicate suffix
    declarations fail immediately instead of depending on registration order.
    """

    languages: tuple[LspLanguageConfig, ...]

    def __post_init__(self) -> None:
        if isinstance(self.languages, (str, bytes)) or not isinstance(self.languages, Sequence):
            raise ValueError("languages must be a sequence of LspLanguageConfig")
        object.__setattr__(self, "languages", tuple(self.languages))
        seen = set()
        for config in self.languages:
            if not isinstance(config, LspLanguageConfig):
                raise ValueError("languages must contain LspLanguageConfig")
            for suffix in config.file_extensions:
                if suffix in seen:
                    raise ValueError(f"Duplicate language suffix: {suffix}")
                seen.add(suffix)

    def select(self, path: str | Path) -> LspLanguageConfig | None:
        name = Path(path).name
        matches = []
        for config in self.languages:
            for suffix in config.file_extensions:
                exact = name.endswith(suffix)
                if exact or (suffix == suffix.lower() and name.lower().endswith(suffix)):
                    matches.append((len(suffix), exact, config))
        return max(matches, key=lambda item: item[:2])[2] if matches else None

    @property
    def description(self) -> str:
        return (
            "; ".join(
                f"{config.language_id} ({', '.join(config.file_extensions)})"
                for config in self.languages
            )
            or "none"
        )


def default_lsp_registry() -> LspRegistry:
    """Build defaults without locating executables or starting subprocesses."""
    typescript = ("typescript-language-server", "--stdio")
    clangd = ("clangd", "--background-index=false", "--clang-tidy=false", "-j=1")
    return LspRegistry(
        (
            LspLanguageConfig(
                "pylsp", "python", (".py", ".pyi"), (sys.executable, "-I", "-m", "pylsp")
            ),
            LspLanguageConfig(
                "typescript-language-server", "javascript", (".js", ".mjs", ".cjs"), typescript
            ),
            LspLanguageConfig(
                "typescript-language-server", "javascriptreact", (".jsx",), typescript
            ),
            LspLanguageConfig(
                "typescript-language-server", "typescript", (".ts", ".mts", ".cts"), typescript
            ),
            LspLanguageConfig(
                "typescript-language-server", "typescriptreact", (".tsx",), typescript
            ),
            LspLanguageConfig("gopls", "go", (".go",), ("gopls", "serve"), 30),
            LspLanguageConfig("clangd", "c", (".c", ".h"), clangd),
            LspLanguageConfig("clangd", "cuda", (".cu", ".cuh"), clangd, 30),
            LspLanguageConfig(
                "clangd", "cpp", (".cpp", ".cc", ".cxx", ".C", ".hpp", ".hh", ".hxx"), clangd
            ),
        )
    )
