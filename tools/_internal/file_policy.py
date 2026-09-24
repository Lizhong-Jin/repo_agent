"""Shared path protection. Local file checks are not an OS sandbox."""

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePath

PROTECTED_NAMES = frozenset(
    {
        ".git",
        ".repo-agent-install.json",
        ".repo-agent-install-transaction",
        ".repo-agent-operation.lock",
        ".codex",
        ".agents",
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        ".git-credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "logs",
    }
)
PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def is_protected_name(path: str | PurePath) -> bool:
    return any(
        part.lower() in PROTECTED_NAMES
        or part.lower() == ".env"
        or part.lower().startswith(".env.")
        or part.lower().endswith(PROTECTED_SUFFIXES)
        for part in PurePath(path).parts
    )


def session_state_root() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base.expanduser().resolve() / "repo-agent/sessions"


def runtime_protected_paths(root=None):
    base = Path.cwd() if root is None else Path(root)
    return [session_state_root(), *(
        (base / value).resolve()
        for key in ("AGENT_ENV_FILE", "AGENT_LOG_DIR")
        if (value := os.environ.get(key))
    )]


_UNSET = object()


@dataclass(frozen=True)
class PathPolicy:
    """An operation-scoped policy snapshot; file metadata is never cached here."""

    protected_paths: tuple[Path, ...] = field(
        default_factory=lambda: tuple(runtime_protected_paths())
    )

    def protects_path(self, requested: Path, resolved: Path) -> bool:
        return any(is_protected_name(path) for path in (requested, resolved)) or any(
            resolved == target or resolved.is_relative_to(target)
            for target in self.protected_paths
        )

    def is_protected(self, requested: Path, resolved: Path, *, info=_UNSET) -> bool:
        """info, when supplied, must describe the resolved target (None if missing)."""
        if self.protects_path(requested, resolved):
            return True
        if info is _UNSET:
            try:
                info = resolved.stat()
            except (FileNotFoundError, NotADirectoryError):
                info = None
        return info is not None and stat.S_ISREG(info.st_mode) and info.st_nlink > 1


def is_credential_path(requested: Path, resolved: Path) -> bool:
    """Compatibility name: now protects credentials, metadata and agent state.

    Check aliases as well as targets. Reject all multiply linked regular files,
    because a harmless filename cannot establish that the other links are safe.
    Configured paths include descendants even if the target does not exist yet.
    """
    return PathPolicy().is_protected(requested, resolved)
