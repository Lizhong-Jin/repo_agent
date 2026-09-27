"""Atomic byte replacement; callers retain serialization, conflicts and transactions."""

import os
import tempfile
import uuid
from pathlib import Path

from .filesystem import open_directory, open_file, rename_at, set_file_mode, unlink_at


def atomic_write(path, content, *, prefix=".state-", sync=False, mode=None, before_replace=None):
    path = Path(path)
    if os.name == "nt":
        return _windows_atomic_write(path, content, prefix, sync, mode, before_replace)
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            if mode is not None:
                os.fchmod(output.fileno(), mode)
            if sync:
                os.fsync(output.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _windows_atomic_write(path, content, prefix, sync, mode, before_replace):
    # Keep the directory pinned through both publication and failure cleanup.
    parent = open_directory(path.parent)
    temporary = prefix + uuid.uuid4().hex
    created = False
    try:
        fd = open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=parent)
        created = True
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            if mode is not None:
                set_file_mode(output.fileno(), mode)
            if sync:
                os.fsync(output.fileno())
        if before_replace is not None:
            before_replace()
        rename_at(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent, replace=True)
    finally:
        try:
            if created:
                try:
                    unlink_at(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent)
