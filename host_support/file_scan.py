"""Scoped directory-reading contract, independent of search and sandbox policy.

A reader is valid only inside its provider's context. Metadata is an observation,
not permission to read later: open_read must validate the object actually opened.
"""

import os
from contextlib import AbstractContextManager
from itertools import islice
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

    def stat_many(self, names: list[str]) -> list[os.stat_result | OSError]: ...

    def open_read(self, name: str) -> AbstractContextManager[BinaryIO]: ...


class DirectorySource(Protocol):
    def scan_directories(self) -> AbstractContextManager[None]: ...

    def read_directory(self, path: Path) -> AbstractContextManager[DirectoryReader]: ...


def metadata_entries(reader: DirectoryReader, names, *, batch_size=128):
    """Bound lookahead; preserve order and report individual metadata failures.

    Older/custom readers can implement only stat. A batch never grants read
    permission: consumers must still validate the file actually opened.
    """
    iterator = iter(names)
    while batch := list(islice(iterator, batch_size)):
        method = getattr(reader, "stat_many", None)
        if method is not None:
            results = method(batch)
        else:
            results = []
            for name in batch:
                try:
                    results.append(reader.stat(name))
                except OSError as error:
                    results.append(error)
        yield from zip(batch, results, strict=True)
