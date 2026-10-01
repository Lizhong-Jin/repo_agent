"""Scoped directory-reading contract, independent of search and sandbox policy.

A reader is valid only inside its provider's context. Metadata is an observation,
not permission to read later: open_read must validate the object actually opened.
"""

import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO, Protocol


class DirectoryReader(Protocol):
    """Single-entry names only; stat and open_read must not follow symlinks.

    open_read validates the actual file against the provider's access policy.
    A pinned directory's identity stays stable, but its entries can still change.
    """

    path: Path

    def names(self) -> list[str]: ...

    def stat(self, name: str) -> os.stat_result: ...

    def open_read(self, name: str) -> AbstractContextManager[BinaryIO]: ...


class DirectorySource(Protocol):
    def read_directory(self, path: Path) -> AbstractContextManager[DirectoryReader]: ...
