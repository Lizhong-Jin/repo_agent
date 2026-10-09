"""Filtered per-call Windows workspaces and conflict-checked publication."""

import os
import shutil
import stat
from pathlib import Path

from host_support.cancellation import checkpoint
from host_support.filesystem import open_file, stat_at, walk_descriptors
from host_support.windows_files import validate_snapshot_names
from tools._internal.file_policy import is_protected_name

from .policy import SandboxPolicy
from .session import SandboxSession, files, fingerprint


def copy_private_tree(source, destination, *, allowed=None, limit=2 * 1024**3):
    """Copy bytes via safe descriptors before launch; never preserve links/ACLs."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    for directory, dirs, names, fd in walk_descriptors(source):
        checkpoint()
        parent = Path(directory).relative_to(source)
        for name in list(dirs) + names:
            relative = (parent / name).as_posix()
            if is_protected_name(relative) or (allowed is not None and not allowed(relative)):
                if name in dirs:
                    dirs.remove(name)
                continue
            validate_snapshot_names([relative])
            info = stat_at(name, dir_fd=fd)
            target = destination / relative
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(exist_ok=True)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Runtime contains a link or special file: {relative}")
            source_fd = open_file(name, dir_fd=fd)
            with os.fdopen(source_fd, "rb") as incoming, target.open("xb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Windows native runtime exceeds the copy size limit")
                    outgoing.write(chunk)
                    checkpoint()


class WindowsCallSnapshot(SandboxSession):
    """Reuse descriptor writeback/backup semantics without a Docker/Git session."""

    def __init__(self, root, workspace, directory, *, protected_paths=()):
        self.root, self.workspace, self.directory = map(Path, (root, workspace, directory))
        self.directory.mkdir(parents=True)
        try:
            self._populate(protected_paths)
        except BaseException:
            self.discard_control()
            raise

    def _populate(self, protected_paths):
        self.workspace.mkdir()
        self.policy = SandboxPolicy()
        self.protected = set()
        self.last_backup = None
        for path in protected_paths:
            if path == self.root or self.root.is_relative_to(path):
                raise ValueError("Windows native workspace overlaps a protected directory")
            if path.is_relative_to(self.root):
                self.protected.add(path.relative_to(self.root).as_posix().rstrip("/") + "/")
        source = files(self.root, self.policy, strict=True, protected=self.protected)
        self.baseline = {name: fingerprint(*entry) for name, entry in source.items()}
        # Keep empty working directories too; prune excluded/reparse directories.
        for directory_name, dirs, _, _ in walk_descriptors(self.root):
            relative = Path(directory_name).relative_to(self.root)
            dirs[:] = [name for name in dirs if not self._excluded((relative / name).as_posix())]
            (self.workspace / relative).mkdir(parents=True, exist_ok=True)
        for name, (data, _) in source.items():
            target = self.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self._save()

    def _excluded(self, name):
        return self.policy.excluded(name) or any(
            name == item.rstrip("/") or name.startswith(item) for item in self.protected
        )

    def publish(self):
        current, changed = self.changes()
        return self._apply_snapshot(current, changed, self.baseline)

    def discard_control(self):
        # This tree is host-only, contains our state/backups, and was never exposed
        # to the LPAC. Incomplete writebacks deliberately retain it instead.
        shutil.rmtree(self.directory)
