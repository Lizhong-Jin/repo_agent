"""Reusable metadata inspection and search traversal; callers own scan budgets."""

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG

from host_support.file_scan import DirectoryReader, DirectorySource, metadata_entries

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


@dataclass(frozen=True)
class SearchCandidate:
    """Read immediately; directory is borrowed until iteration advances/closes."""

    path: Path
    info: os.stat_result | None
    directory: DirectoryReader | None = None


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
    access = current_file_access()
    info = access.stat(target) if access else target.stat()
    if S_ISREG(info.st_mode):
        if glob is None or fnmatch(relative.as_posix(), glob):
            yield SearchCandidate(target, info)
        return
    if not S_ISDIR(info.st_mode):
        return
    if access:
        with access.scan_directories():
            yield from _native_search_candidates(
                access,
                workspace_root,
                target,
                include_hidden=include_hidden,
                glob=glob,
                policy=policy,
            )
        return
    walk = os.walk(target, followlinks=False)
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
                info = candidate.lstat()
            except OSError:
                # Let the caller count an unreadable candidate as a skipped file.
                info = None
            if info is not None and S_ISLNK(info.st_mode):
                continue
            yield SearchCandidate(candidate, info)


def _native_search_candidates(
    access: DirectorySource,
    workspace_root: Path,
    target: Path,
    *,
    include_hidden: bool,
    glob: str | None,
    policy: PathPolicy,
):
    """One scoped reader per directory; reuse enumeration metadata for reads.

    Keep file order, directory pruning and unreadable-entry behavior identical
    to the descriptor walk. The source bounds directory handles within this scan.
    """
    pending = [target]
    while pending:
        root = pending.pop()
        children = []
        try:
            with access.read_directory(root) as directory:
                names = sorted(directory.names(), key=lambda name: (name.casefold(), name))
                if not include_hidden:
                    names = [name for name in names if not name.startswith(".")]
                for name, info in metadata_entries(directory, names):
                    candidate = root / name
                    if isinstance(info, OSError):
                        continue
                    if S_ISDIR(info.st_mode):
                        if not policy.is_protected(candidate, candidate.resolve(), info=info):
                            children.append(candidate)
                        continue
                    if S_ISLNK(info.st_mode):
                        continue
                    relative = candidate.relative_to(workspace_root).as_posix()
                    if glob is None or fnmatch(relative, glob):
                        yield SearchCandidate(candidate, info, directory)
        except OSError:
            # Directory access failures were also skipped by FileAccess.walk.
            continue
        pending.extend(reversed(children))
