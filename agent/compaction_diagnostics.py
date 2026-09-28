"""Private, exclusive diagnostic files; never put summary candidates in trace logs."""

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

from host_support.filesystem import (
    mkdir_at,
    open_directory,
    open_file,
    set_file_mode,
    unlink_at,
)

from .session import SESSION_ID


def save_diagnostic(store, data):
    if not SESSION_ID.fullmatch(store.id):
        raise ValueError("Invalid diagnostic session")
    name = uuid4().hex
    payload = json.dumps(
        {"version": 1, "created_at": datetime.now(UTC).isoformat(), "session_id": store.id, **data},
        ensure_ascii=False,
        allow_nan=False,
    )
    # The already established project state directory is outside ordinary file-tool access.
    parent = open_directory(store.directory)
    try:
        for component in (store.id, "compaction-diagnostics"):
            try:
                mkdir_at(component, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = open_directory(component, dir_fd=parent)
            os.close(parent)
            parent = child
            set_file_mode(parent, 0o700)
        filename = name + ".json"
        fd = open_file(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode=0o600,
            dir_fd=parent,
            nonblocking=False,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            unlink_at(filename, dir_fd=parent)
            raise
    finally:
        os.close(parent)
    return name
