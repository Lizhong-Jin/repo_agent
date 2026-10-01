"""Line iteration for bounded, fully decoded search snapshots."""

import re
from collections.abc import Iterator


def text_lines(text: str) -> Iterator[str]:
    """Match CRLF/CR/LF normalization + split('\n') without a full line list.

    Unlike str.splitlines(), Unicode separators remain part of the same line.
    The caller already decoded a bounded, verified snapshot in its entirety.
    """
    start = 0
    for match in re.finditer(r"\r\n?|\n", text):
        yield text[start : match.start()]
        start = match.end()
    yield text[start:]
