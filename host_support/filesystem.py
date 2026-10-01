"""Descriptor-relative I/O for trusted native file tools, without spawning a process.

This is an application boundary, not a sandbox for arbitrary code. Never execute
project code in this scope. ContextVar keeps concurrent calls' policies separate.
"""

import errno
import os
import stat
import sys
import uuid
from collections import OrderedDict
from contextlib import closing, contextmanager
from contextvars import ContextVar
from fnmatch import fnmatchcase
from pathlib import Path

from .cancellation import checkpoint
from .file_scan import DirectoryReader, metadata_entries

_scan_directories = ContextVar("scan_directories", default=None)
_SCAN_DIRECTORY_LIMIT = 32

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

    def __init__(
        self, root, *, policy, protected_paths=(), read_only_paths=(), directory_backend=None
    ):
        self.root = Path(root)
        self.directory_backend = directory_backend
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
        if self.directory_backend is not None:
            fd = self.directory_backend.directory(self.root_fd, parts)
            try:
                yield fd
            finally:
                os.close(fd)
            return
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

    def _directory_names(self, fd):
        if self.directory_backend is not None:
            return self.directory_backend.names(fd)
        return list_directory(fd)

    def iterdir(self, path):
        with self.directory(path) as fd:
            names = self._directory_names(fd)
        for name in names:
            yield Path(path) / name

    @contextmanager
    def scan_directories(self):
        """Bounded, read-only handle reuse, isolated to this traversal's context.

        Cached handles pin identities; they are not fresh path observations.
        Writes and standalone directory/stat calls never consult this cache.
        """
        cache = _ScanDirectories(self)
        token = _scan_directories.set(cache)
        try:
            yield
        finally:
            _scan_directories.reset(token)
            cache.close()

    def iterdir_entries(self, path):
        with self.read_directory(path) as reader:
            for name, info in metadata_entries(reader, reader.names()):
                yield Path(path) / name, info

    @contextmanager
    def read_directory(self, path):
        """Pin one directory for a batch of reads; never cache it across calls."""
        path = Path(path)
        cache = _scan_directories.get()
        source = cache.directory(path) if cache and cache.access is self else self.directory(path)
        with source as fd:
            reader = _DescriptorDirectoryReader(self, path, fd)
            try:
                yield reader
            finally:
                reader.closed = True

    def walk(self, path):
        with self.scan_directories():
            yield from self._walk(path)

    def _walk(self, path):
        pending = [Path(path)]
        while pending:
            root = pending.pop()
            dirs, files = [], []
            try:
                with self.read_directory(root) as directory:
                    for name, info in metadata_entries(directory, directory.names()):
                        if isinstance(info, OSError):
                            continue
                        (dirs if stat.S_ISDIR(info.st_mode) else files).append(name)
            except OSError:
                continue
            yield root, dirs, files
            # Honour pruning/sorting by the caller; never follow directory links.
            pending.extend(root / name for name in reversed(dirs))

    def glob(self, root, pattern):
        with closing(self.glob_entries(root, pattern)) as entries:
            for path, _ in entries:
                yield path

    def glob_entries(self, root, pattern):
        with self.scan_directories():
            yield from self._glob_entries(root, pattern)

    def _glob_entries(self, root, pattern):
        parts = Path(pattern).parts
        if Path(pattern).is_absolute() or ".." in parts:
            raise ValueError("Glob must stay inside the search directory")
        pending = [(Path(root), 0, None)]
        visited = set()
        directories_only = pattern.endswith("/")
        while pending:
            path, index, info = pending.pop()
            if (path, index) in visited:
                continue
            visited.add((path, index))
            if index == len(parts):
                try:
                    info = self.stat(path) if info is None else info
                    if not directories_only or stat.S_ISDIR(info.st_mode):
                        yield path, info
                except OSError:
                    pass
                continue
            part = parts[index]
            if part == "**":
                pending.append((path, index + 1, info))
            try:
                with self.read_directory(path) as directory:
                    for name, info in metadata_entries(directory, directory.names()):
                        entry = path / name
                        if isinstance(info, OSError):
                            continue
                        if part == "**":
                            if stat.S_ISDIR(info.st_mode):
                                pending.append((entry, index, info))
                            elif index == len(parts) - 1 and sys.version_info >= (3, 13):
                                pending.append((entry, index + 1, info))
                        elif fnmatchcase(name, part):
                            if index + 1 == len(parts) or stat.S_ISDIR(info.st_mode):
                                pending.append((entry, index + 1, info))
            except OSError:
                continue

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


class _ScanDirectories:
    """LRU of at most 32 owned descriptors, using the nearest pinned ancestor."""

    def __init__(self, access):
        self.access = access
        self.handles = OrderedDict()

    def close(self):
        while self.handles:
            _, fd = self.handles.popitem()
            os.close(fd)

    @contextmanager
    def directory(self, path):
        parts = self.access._parts(path)
        ancestor = parts
        while ancestor and ancestor not in self.handles:
            ancestor = ancestor[:-1]
        if ancestor:
            parent = self.handles[ancestor]
            self.handles.move_to_end(ancestor)
        else:
            parent = self.access.root_fd
        fd = os.dup(parent)
        try:
            for index in range(len(ancestor), len(parts)):
                checkpoint()
                backend = self.access.directory_backend
                child = (
                    backend.directory(fd, [parts[index]])
                    if backend is not None
                    else open_directory(parts[index], dir_fd=fd)
                )
                os.close(fd)
                fd = child
                key = parts[: index + 1]
                self.handles[key] = os.dup(fd)
                if len(self.handles) > _SCAN_DIRECTORY_LIMIT:
                    _, expired = self.handles.popitem(last=False)
                    os.close(expired)
            yield fd
        finally:
            os.close(fd)


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
            # read_directory already checked the parent is within the root;
            # a validated single component cannot escape it. Keep the child
            # policy check without repeating two relative_to walks per entry.
            path = self.path / name
            if self.access.policy.protects_path(path, path):
                raise PermissionError("Protected path")

    def names(self):
        self._check()
        return self.access._directory_names(self.fd)

    def stat(self, name):
        self._check(name)
        return stat_at(name, dir_fd=self.fd)

    def stat_many(self, names):
        self._check()
        checkpoint()
        results, allowed, positions = [None] * len(names), [], []
        for index, name in enumerate(names):
            try:
                self._check(name)
            except OSError as error:
                results[index] = error
            else:
                allowed.append(name)
                positions.append(index)
        backend = self.access.directory_backend
        if backend is not None:
            observed = backend.stat_many(self.fd, allowed)
        else:
            observed = []
            for name in allowed:
                try:
                    observed.append(stat_at(name, dir_fd=self.fd))
                except OSError as error:
                    observed.append(error)
        for index, info in zip(positions, observed, strict=True):
            results[index] = info
        return results

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
