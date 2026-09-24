"""Small shared foundation for workspace tools; operation semantics stay in each tool."""

from functools import wraps
from pathlib import Path
from threading import RLock

from .file_policy import PathPolicy

# This only coordinates writers in this process, not external editors or workers.
_file_write_lock = RLock()


def serialized_file_write(method):
    @wraps(method)
    def execute(self, arguments):
        with _file_write_lock:
            return method(self, arguments)
    return execute


class WorkspaceTool:
    def __init__(self, workspace_root: str | Path, **limits: int) -> None:
        self.workspace_root = Path(workspace_root).resolve(strict=True)
        if not self.workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        for name, value in limits.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            setattr(self, name, value)

    @staticmethod
    def path_policy() -> PathPolicy:
        # Snapshot configuration per operation, never across tool executions.
        # Keep relative environment paths based on cwd for compatibility.
        return PathPolicy()
