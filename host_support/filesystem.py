"""Descriptor-relative I/O for trusted native file tools, without spawning a process.

This is an application boundary, not a sandbox for arbitrary code. Never execute
project code in this scope. ContextVar keeps concurrent calls' policies separate.
"""

import errno
import os
import stat
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from fnmatch import fnmatchcase
from pathlib import Path

from .file_scan import DirectoryReader

_active_access = ContextVar("native_file_access", default=None)


def require_safe_descriptors():
    if os.name not in {"posix", "nt"}:
        raise OSError(errno.ENOTSUP, "Safe descriptor access is not implemented on this platform")


def open_directory(path, *, dir_fd=None):
    require_safe_descriptors()
    if os.name == "nt":
        from . import windows_files

        return windows_files.open_directory(path, dir_fd=dir_fd)
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def walk_descriptors(root):
    require_safe_descriptors()
    if os.name == "posix":
        yield from os.fwalk(root, follow_symlinks=False)
        return

    def walk(path, fd):
        dirs, names = [], []
        for name in list_directory(fd):
            info = stat_at(name, dir_fd=fd)
            (dirs if stat.S_ISDIR(info.st_mode) else names).append(name)
        yield str(path), dirs, names, fd
        for name in dirs:
            child = open_directory(name, dir_fd=fd)
            try:
                yield from walk(Path(path) / name, child)
            finally:
                os.close(child)

    fd = open_directory(root)
    try:
        yield from walk(root, fd)
    finally:
        os.close(fd)


def open_file(path, flags=os.O_RDONLY, mode=0o600, *, dir_fd=None, nonblocking=True):
    """Pin an entry without following its final link; callers validate type/size."""
    require_safe_descriptors()
    if os.name == "nt":
        from . import windows_files

        return windows_files.open_file(path, flags, mode, dir_fd=dir_fd, nonblocking=nonblocking)
    flags |= os.O_NOFOLLOW
    if nonblocking:
        flags |= os.O_NONBLOCK
    return os.open(path, flags, mode, dir_fd=dir_fd)


def stat_at(path, *, dir_fd):
    if os.name == "nt":
        from . import windows_files

        return windows_files.stat_at(path, dir_fd=dir_fd)
    return os.stat(path, dir_fd=dir_fd, follow_symlinks=False)


def list_directory(fd):
    if os.name == "nt":
        from . import windows_files

        return windows_files.list_directory(fd)
    return os.listdir(fd)


def mkdir_at(path, mode=0o777, *, dir_fd):
    if os.name == "nt":
        from . import windows_files

        return windows_files.mkdir_at(path, mode, dir_fd=dir_fd)
    return os.mkdir(path, mode=mode, dir_fd=dir_fd)


def unlink_at(path, *, dir_fd):
    if os.name == "nt":
        from . import windows_files

        return windows_files.unlink_at(path, dir_fd=dir_fd)
    return os.unlink(path, dir_fd=dir_fd)


def rename_at(source, destination, *, src_dir_fd, dst_dir_fd, replace=False):
    if os.name == "nt":
        from . import windows_files

        return windows_files.rename_at(
            source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, replace=replace
        )
    operation = os.replace if replace else os.rename
    return operation(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


def set_file_mode(fd, mode):
    if os.name == "nt":
        from . import windows_files

        return windows_files.set_mode(fd, mode)
    return os.fchmod(fd, mode)


def current_file_access():
    return _active_access.get()


class FileAccess:
    """Pin directories and refuse link traversal at actual I/O time."""

    def __init__(self, root, *, policy, protected_paths=(), read_only_paths=()):
        self.root = Path(root)
        self.protected_paths = tuple(protected_paths)
        self.read_only_paths = tuple(read_only_paths)
        self.policy = policy

    @contextmanager
    def activate(self):
        self.root_fd = open_directory(self.root)
        token = _active_access.set(self)
        try:
            yield self
        finally:
            _active_access.reset(token)
            os.close(self.root_fd)

    def _parts(self, path, *, write=False):
        path = Path(path)
        if not path.is_relative_to(self.root):
            raise PermissionError("Path outside workspace")
        parts = path.relative_to(self.root).parts
        if ".." in parts or self.policy.protects_path(path, path):
            raise PermissionError("Protected path")
        if write and any(path.is_relative_to(p) for p in self.read_only_paths):
            raise PermissionError("Native runtime paths are read-only")
        return parts

    @contextmanager
    def directory(self, path):
        parts = self._parts(path)
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                child = open_directory(part, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def parent(self, path, *, write=False):
        if not self._parts(path, write=write):
            raise PermissionError("Cannot operate on the workspace root")
        with self.directory(Path(path).parent) as fd:
            yield fd, Path(path).name

    @staticmethod
    def regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Expected an independent regular file")

    @contextmanager
    def open_read(self, path):
        with self.parent(path) as (parent, name):
            fd = open_file(name, dir_fd=parent)
        try:
            self.regular(os.fstat(fd))
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            yield stream

    def stat(self, path, *, follow_symlinks=False):
        if Path(path) == self.root:
            return os.fstat(self.root_fd)
        # Callers resolve allowed aliases before content access. Metadata can
        # describe a link, but must never follow a replacement link here.
        with self.parent(path) as (parent, name):
            info = stat_at(name, dir_fd=parent)
        if follow_symlinks and stat.S_ISLNK(info.st_mode):
            raise PermissionError("Path changed to a symbolic link")
        return info

    def iterdir(self, path):
        with self.directory(path) as fd:
            names = list_directory(fd)
        for name in names:
            yield Path(path) / name

    @contextmanager
    def read_directory(self, path):
        """Pin one directory for a batch of reads; never cache it across calls."""
        path = Path(path)
        with self.directory(path) as fd:
            reader = _DescriptorDirectoryReader(self, path, fd)
            try:
                yield reader
            finally:
                reader.closed = True

    def walk(self, path):
        pending = [Path(path)]
        while pending:
            root = pending.pop()
            dirs, files = [], []
            try:
                for entry in self.iterdir(root):
                    try:
                        info = self.stat(entry)
                    except OSError:
                        continue
                    (dirs if stat.S_ISDIR(info.st_mode) else files).append(entry.name)
            except OSError:
                continue
            yield root, dirs, files
            # Honour pruning/sorting by the caller; never follow directory links.
            pending.extend(root / name for name in reversed(dirs))

    def glob(self, root, pattern):
        parts = Path(pattern).parts
        if Path(pattern).is_absolute() or ".." in parts:
            raise ValueError("Glob must stay inside the search directory")
        pending = [(Path(root), 0)]
        visited = set()
        directories_only = pattern.endswith("/")
        while pending:
            path, index = pending.pop()
            if (path, index) in visited:
                continue
            visited.add((path, index))
            if index == len(parts):
                try:
                    info = self.stat(path)
                    if not directories_only or stat.S_ISDIR(info.st_mode):
                        yield path
                except OSError:
                    pass
                continue
            part = parts[index]
            if part == "**":
                pending.append((path, index + 1))
            try:
                entries = list(self.iterdir(path))
            except OSError:
                continue
            for entry in entries:
                try:
                    info = self.stat(entry)
                except OSError:
                    continue
                if part == "**":
                    if stat.S_ISDIR(info.st_mode):
                        pending.append((entry, index))
                    elif index == len(parts) - 1 and sys.version_info >= (3, 13):
                        # Match pathlib's Python 3.13+ terminal ** behaviour.
                        pending.append((entry, index + 1))
                elif fnmatchcase(entry.name, part):
                    if index + 1 == len(parts) or stat.S_ISDIR(info.st_mode):
                        pending.append((entry, index + 1))

    def mkdir(self, path, *, parents=False, exist_ok=False):
        parts = self._parts(path, write=True)
        if not parts:
            if not exist_ok:
                raise FileExistsError(str(path))
            return
        fd = os.dup(self.root_fd)
        try:
            for index, part in enumerate(parts):
                final = index == len(parts) - 1
                if parents or final:
                    try:
                        mkdir_at(part, dir_fd=fd)
                    except FileExistsError:
                        if final and not exist_ok:
                            raise
                child = open_directory(part, dir_fd=fd)
                os.close(fd)
                fd = child
        finally:
            os.close(fd)

    def unlink(self, path):
        with self.parent(path, write=True) as (fd, name):
            self.regular(stat_at(name, dir_fd=fd))
            unlink_at(name, dir_fd=fd)

    def rename(self, source, destination):
        with self.parent(source, write=True) as (src, src_name):
            with self.parent(destination, write=True) as (dst, dst_name):
                self.regular(stat_at(src_name, dir_fd=src))
                try:
                    stat_at(dst_name, dir_fd=dst)
                except FileNotFoundError:
                    pass
                else:
                    raise FileExistsError(str(destination))
                rename_at(src_name, dst_name, src_dir_fd=src, dst_dir_fd=dst)

    def stage(self, target, content, mode):
        with self.parent(target, write=True) as (parent, _):
            fd = os.dup(parent)
        name = ".repo-agent-write-" + uuid.uuid4().hex
        staged = _StagedFile(self, fd, name)
        try:
            out = open_file(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=fd, nonblocking=False
            )
            with os.fdopen(out, "wb") as stream:
                stream.write(content)
                stream.flush()
                if mode is not None:
                    set_file_mode(stream.fileno(), mode & 0o777)
                os.fsync(stream.fileno())
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise


class _DescriptorDirectoryReader(DirectoryReader):
    """Directory-local operations with the same policy and opened-file checks."""

    def __init__(self, access, path, fd):
        self.access, self.path, self.fd = access, path, fd
        self.closed = False

    def _check(self, name=None):
        if self.closed:
            raise ValueError("Directory reader is closed")
        if name is not None:
            if name in {"", ".", ".."} or Path(name).name != name or "\x00" in name:
                raise ValueError("Expected a single directory entry name")
            self.access._parts(self.path / name)

    def names(self):
        self._check()
        return list_directory(self.fd)

    def stat(self, name):
        self._check(name)
        return stat_at(name, dir_fd=self.fd)

    @contextmanager
    def open_read(self, name):
        self._check(name)
        fd = open_file(name, dir_fd=self.fd)
        try:
            self.access.regular(os.fstat(fd))
            stream = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            yield stream


class _StagedFile:
    """Staging location stays pinned through replacement and failure cleanup."""

    def __init__(self, access, parent, name):
        self.access, self.parent, self.name = access, parent, name

    def replace(self, target):
        with self.access.parent(target, write=True) as (fd, name):
            before, current = os.fstat(self.parent), os.fstat(fd)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise PermissionError("Destination directory changed during write")
            try:
                info = stat_at(name, dir_fd=fd)
            except FileNotFoundError:
                pass
            else:
                self.access.regular(info)
            rename_at(self.name, name, src_dir_fd=self.parent, dst_dir_fd=fd, replace=True)

    def unlink(self, *, missing_ok=False):
        if self.parent is None:
            return
        try:
            unlink_at(self.name, dir_fd=self.parent)
        except FileNotFoundError:
            if not missing_ok:
                raise
        finally:
            os.close(self.parent)
            self.parent = None
