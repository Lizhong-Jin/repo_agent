"""Workspace policy adapter for the shared descriptor-based file service."""

from host_support.filesystem import FileAccess as DescriptorFileAccess
from host_support.filesystem import current_file_access as current_file_access

from .file_policy import PathPolicy


class FileAccess(DescriptorFileAccess):
    def __init__(self, root, *, protected_paths=(), read_only_paths=()):
        protected_paths = tuple(protected_paths)
        super().__init__(
            root,
            policy=PathPolicy(protected_paths=protected_paths),
            protected_paths=protected_paths,
            read_only_paths=read_only_paths,
        )
