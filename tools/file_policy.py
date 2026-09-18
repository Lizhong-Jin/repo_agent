"""Shared path protection. Local file checks are not an OS sandbox."""

import os
import stat
from pathlib import Path, PurePath

PROTECTED_NAMES = frozenset(
    {
        ".git",
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


def is_credential_path(requested: Path, resolved: Path) -> bool:
    """Compatibility name: now protects credentials, metadata and agent state.

    Check aliases as well as targets. Reject all multiply linked regular files,
    because a harmless filename cannot establish that the other links are safe.
    Configured paths include descendants even if the target does not exist yet.
    """
    if any(is_protected_name(path) for path in (requested, resolved)):
        return True
    for key in ("AGENT_ENV_FILE", "AGENT_LOG_DIR"):
        custom = os.environ.get(key)
        if custom:
            target = Path(custom).resolve()
            if resolved == target or resolved.is_relative_to(target):
                return True
    try:
        info = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink > 1
