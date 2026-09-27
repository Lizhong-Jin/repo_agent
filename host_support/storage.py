"""Atomic byte replacement; callers retain serialization, conflicts and transactions."""

import os
import tempfile
from pathlib import Path


def atomic_write(path, content, *, prefix=".state-", sync=False, mode=None, before_replace=None):
    path = Path(path)
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
