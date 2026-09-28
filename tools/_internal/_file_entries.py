"""Reusable metadata inspection and search traversal; callers own scan budgets."""

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG

from .file_access import current_file_access
from .file_policy import PathPolicy


@dataclass(frozen=True)
class FileEntry:
    path: Path
    resolved: Path
    info: os.stat_result

    @property
    def kind(self) -> str:
        if S_ISLNK(self.info.st_mode):
            return "symlink"
        if S_ISDIR(self.info.st_mode):
            return "directory"
        if S_ISREG(self.info.st_mode):
            return "file"
        return "other"


def inspect_entry(
    path: Path,
    policy: PathPolicy,
    *,
    info: os.stat_result | None = None,
) -> FileEntry | None:
    """Resolve once and reuse lstat for type, size and hard-link protection.

    Supplied metadata must come from lstat (or a known resolved, non-symlink path).
    Symlinks require separate target metadata, including dangling-link handling.
    """
    resolved = path.resolve()
    if policy.protects_path(path, resolved):
        return None
    if info is None:
        access = current_file_access()
        info = access.stat(path) if access else path.lstat()
    target_info = info
    if S_ISLNK(info.st_mode):
        try:
            target_info = resolved.stat()
        except (FileNotFoundError, NotADirectoryError):
            target_info = None
    if policy.is_protected(path, resolved, info=target_info):
        return None
    return FileEntry(path, resolved, info)


def iter_search_candidates(
    workspace_root: Path,
    target: Path,
    *,
    include_hidden: bool,
    glob: str | None,
    policy: PathPolicy,
):
    relative = target.relative_to(workspace_root)
    if not include_hidden and any(part.startswith(".") for part in relative.parts):
        return
    info = target.stat()
    if S_ISREG(info.st_mode):
        if glob is None or fnmatch(relative.as_posix(), glob):
            yield target, info
        return
    if not S_ISDIR(info.st_mode):
        return
    access = current_file_access()
    walk = access.walk(target) if access else os.walk(target, followlinks=False)
    for root, dirnames, filenames in walk:
        dirnames.sort(key=lambda name: (name.casefold(), name))
        filenames.sort(key=lambda name: (name.casefold(), name))
        root_path = Path(root)
        if not include_hidden:
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            filenames = [name for name in filenames if not name.startswith(".")]
        dirnames[:] = [
            name
            for name in dirnames
            if not policy.is_protected(root_path / name, (root_path / name).resolve())
        ]
        for name in filenames:
            candidate = root_path / name
            relative = candidate.relative_to(workspace_root).as_posix()
            if glob is not None and not fnmatch(relative, glob):
                continue
            try:
                info = access.stat(candidate) if access else candidate.lstat()
            except OSError:
                # Let the caller count an unreadable candidate as a skipped file.
                info = None
            if info is not None and S_ISLNK(info.st_mode):
                continue
            yield candidate, info
