"""Bounded byte snapshots and staged atomic replacements, independent of text formats."""

import hashlib
import logging
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from tempfile import NamedTemporaryFile

from host_support.file_scan import DirectoryReader, ScanMetadata
from host_support.read_budget import bounded_read

from .base import ToolResult
from .errors import ToolErrorCode, tool_error
from .file_access import current_file_access


def snapshot_stat(path: Path, *, follow_symlinks: bool = False) -> os.stat_result:
    """Use the same metadata backend as the subsequent descriptor read.

    On Windows, path stat's ctime can mean creation time while handle stat's
    ctime means change time. Never mix those versions in a snapshot signature.
    """
    access = current_file_access()
    return (
        access.stat(path, follow_symlinks=follow_symlinks)
        if access
        else path.stat(follow_symlinks=follow_symlinks)
    )


def file_signature(info: os.stat_result | ScanMetadata) -> tuple[int, ...]:
    # Windows path stat infers execute bits from .exe/.bat extensions; handle
    # stat does not. They describe the same file and must compare consistently.
    mode = info.st_mode & ~0o111 if os.name == "nt" else info.st_mode
    return (
        info.st_dev,
        info.st_ino,
        mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@dataclass(frozen=True)
class FileSnapshot:
    target: Path
    raw: bytes
    info: os.stat_result

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def signature(self) -> tuple[int, ...]:
        return file_signature(self.info)


def read_snapshot(
    candidate: Path,
    target: Path,
    info: os.stat_result | ScanMetadata,
    max_bytes: int,
    *,
    verify_identity: bool = False,
    size_message: str | None = None,
    directory: DirectoryReader | None = None,
) -> FileSnapshot | ToolResult:
    """Caller checks path policy and regular-file type, and maps I/O exceptions.

    Identity verification preserves the strict patch reader's before/after checks.
    Plain readers retain their existing symlink policy. No text decoding occurs here.
    """
    if info.st_size > max_bytes:
        return tool_error(ToolErrorCode.FILE_TOO_LARGE, size_message)
    access = current_file_access()
    if directory is not None and (access is None or directory.path != target.parent):
        raise ValueError("Directory reader must belong to the native target's parent")
    if access:
        opened = directory.open_read(target.name) if directory else access.open_read(target)
        with opened as source:
            before = os.fstat(source.fileno())
            if file_signature(before) != file_signature(info):
                return tool_error(ToolErrorCode.FILE_CHANGED)
            raw = bounded_read(source, max_bytes + 1, info.st_size)
            after = os.fstat(source.fileno())
            access.regular(after)
        if file_signature(before) != file_signature(after):
            return tool_error(ToolErrorCode.FILE_CHANGED)
        info = after
    elif verify_identity:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(target, flags)
        try:
            source = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with source:
            before = os.fstat(source.fileno())
            if file_signature(before) != file_signature(info):
                return tool_error(ToolErrorCode.FILE_CHANGED)
            raw = bounded_read(source, max_bytes + 1, info.st_size)
            after = os.fstat(source.fileno())
        if file_signature(before) != file_signature(after) or file_signature(
            candidate.lstat()
        ) != file_signature(after):
            return tool_error(ToolErrorCode.FILE_CHANGED)
        info = after
    else:
        with target.open("rb") as source:
            before = os.fstat(source.fileno())
            raw = bounded_read(source, max_bytes + 1, before.st_size)
            after = os.fstat(source.fileno())
        if file_signature(before) != file_signature(after):
            return tool_error(ToolErrorCode.FILE_CHANGED)
        info = after
    if len(raw) > max_bytes:
        return tool_error(ToolErrorCode.FILE_TOO_LARGE, size_message)
    return FileSnapshot(target=target, raw=raw, info=info)


class StagedWrites:
    """Own temporary files through success, failure and cancellation.

    Each replacement is atomic; a group of replacements is not a transaction.
    Callers own conflict checks and partial-commit reporting.
    """

    def __init__(self, *, temp_factory=NamedTemporaryFile, logger=None) -> None:
        self._temp_factory = temp_factory
        self._logger = logger or logging.getLogger(__name__)
        self._paths: list[Path] = []

    def __enter__(self):
        return self

    def stage(self, target: Path, content: bytes, mode: int | None = None) -> Path:
        access = current_file_access()
        if access:
            staged = access.stage(target, content, mode)
            self._paths.append(staged)
            return staged
        with self._temp_factory(mode="wb", dir=target.parent, delete=False) as temp:
            path = Path(temp.name)
            self._paths.append(path)  # Register before write/flush/fsync can fail.
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        if mode is not None:
            os.chmod(path, mode)
        return path

    def replace(self, target: Path, content: bytes, mode: int | None = None) -> None:
        self.stage(target, content, mode).replace(target)

    def __exit__(self, *_exc) -> None:
        for path in self._paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self._logger.warning("Unable to remove temporary file %s", path, exc_info=True)
