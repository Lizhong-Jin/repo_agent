"""Line iteration for bounded, fully decoded search snapshots."""

import re
from collections.abc import Iterator

from host_support.read_budget import read_checkpoint


def source_line_count(text: str) -> int:
    """Count CRLF/CR/LF in bounded snapshots with no normalization copies."""
    read_checkpoint()
    count = text.count("\n") + text.count("\r") - text.count("\r\n")
    count += bool(text) and not text.endswith(("\n", "\r"))
    read_checkpoint()
    return count


def source_line_spans(text: str) -> Iterator[tuple[int, int]]:
    """Source lines without copies or a phantom line after a final newline."""
    start = 0
    for match in re.finditer(r"\r\n?|\n", text):
        read_checkpoint()
        yield start, match.start()
        start = match.end()
    read_checkpoint()
    if start < len(text):
        yield start, len(text)


def text_lines(text: str) -> Iterator[str]:
    """Match CRLF/CR/LF normalization + split('\n') without a full line list.

    Unlike str.splitlines(), Unicode separators remain part of the same line.
    The caller already decoded a bounded, verified snapshot in its entirety.
    """
    start = 0
    for match in re.finditer(r"\r\n?|\n", text):
        read_checkpoint()
        yield text[start : match.start()]
        start = match.end()
    yield text[start:]


def literal_span(text: str, query: str, *, case_sensitive: bool) -> tuple[int, int] | None:
    """First literal match as an original-text span, including casefold expansions."""
    folded = text if case_sensitive else text.casefold()
    needle = query if case_sensitive else query.casefold()
    start = folded.find(needle)
    if start < 0:
        return None
    end = start + len(needle)
    if case_sensitive or text.isascii():
        return start, end
    # Only matching non-ASCII lines need mapping. No per-character index array.
    offset = 0
    original_start = None
    for index, char in enumerate(text):
        if index % 1024 == 0:
            read_checkpoint()
        next_offset = offset + len(char.casefold())
        if original_start is None and next_offset > start:
            original_start = index
        if next_offset >= end:
            return original_start, index + 1
        offset = next_offset
    raise AssertionError("Casefold span did not map to source text")
