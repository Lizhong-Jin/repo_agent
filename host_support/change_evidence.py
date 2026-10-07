"""Bounded, byte-derived evidence; never infer authorship from a directory diff."""

import difflib
import hashlib

TEXT_LIMIT = 64 * 1024
DIFF_LIMIT = 32 * 1024


def content_record(raw):
    if raw is None:
        return {"exists": False}
    record = {"exists": True, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}
    if len(raw) <= TEXT_LIMIT and b"\0" not in raw:
        try:
            record["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return record


def difference(before, after, path):
    if (
        (before.get("sha256") and before.get("sha256") == after.get("sha256"))
        or before.get("exists") is False
        and after.get("exists") is False
    ):
        return {"diff": "", "diff_status": "unchanged"}
    a = before.get("text", "" if before.get("exists") is False else None)
    b = after.get("text", "" if after.get("exists") is False else None)
    if a is None or b is None:
        return {"diff": "", "diff_status": "unavailable"}
    # Bound both input and output, even for huge one-line replacements.
    parts, size = [], 0
    for line in difflib.unified_diff(
        a.splitlines(keepends=True),
        b.splitlines(keepends=True),
        fromfile=f"before/{path}",
        tofile=f"after/{path}",
    ):
        remaining = DIFF_LIMIT - size
        parts.append(line[:remaining])
        size += len(line)
        if size > DIFF_LIMIT:
            return {"diff": "".join(parts), "diff_status": "truncated"}
    return {"diff": "".join(parts), "diff_status": "available"}


def file_change(path, before, after):
    """Called with bytes actually consumed/produced by a controlled file tool.

    The input is what the tool read, not a claim of exclusive directory ownership.
    A missing before-image is explicitly unknown, never an invented empty file.
    """
    return {
        "path": path,
        "before": {k: v for k, v in before.items() if k != "text"},
        "after": {k: v for k, v in after.items() if k != "text"},
        **difference(before, after, path),
        "basis": "controlled_file_operation",
    }
