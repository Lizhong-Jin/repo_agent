"""Descriptor locks. Keep lock files in place so concurrent users share an inode."""

import errno
import os


def lock_descriptor(fd, *, blocking=True):
    if os.name != "posix":
        raise OSError(errno.ENOTSUP, "Safe file locking is not implemented on this platform")
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
