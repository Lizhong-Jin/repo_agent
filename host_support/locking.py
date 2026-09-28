"""Descriptor locks. Keep lock files in place so concurrent users share an inode."""

import errno
import os
from contextlib import contextmanager

from .filesystem import open_file


def lock_descriptor(fd, *, blocking=True):
    if os.name == "nt":
        from . import windows_files

        return windows_files.lock_descriptor(fd, blocking=blocking)
    if os.name != "posix":
        raise OSError(errno.ENOTSUP, "Safe file locking is not implemented on this platform")
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = open_file(path, os.O_RDWR | os.O_CREAT, nonblocking=False)
    try:
        try:
            lock_descriptor(fd, blocking=False)
        except BlockingIOError:
            raise ValueError(f"另一个安装、卸载或配置操作正在进行，请稍后重试：{path}") from None
        yield
    finally:
        os.close(fd)  # Keep the inode: unlinking a lock file can split concurrent locks.
