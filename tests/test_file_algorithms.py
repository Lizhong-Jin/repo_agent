"""Equivalent ordering/line semantics with bounded intermediate allocations."""

import random

import pytest

from tools import ListFileTool, ReadFileTool
from tools._internal.selection import SmallestItems
from tools._internal.text_search import source_line_count, source_line_spans


@pytest.mark.parametrize("text", ["", "\n", "\r", "a\r\n\r\nb", "a\nb\n", "a\rb", "a\u2028b\n"])
def test_source_spans_match_source_line_semantics(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    expected = normalized.split("\n") if normalized else []
    if normalized.endswith("\n"):
        expected.pop()
    assert source_line_count(text) == len(expected)
    assert [text[left:right] for left, right in source_line_spans(text)] == expected


def test_narrow_read_still_counts_tail_without_skipping_a_full_line(tmp_path):
    (tmp_path / "a").write_bytes("x\r\n长".encode() + b"z" * 100 + b"\ry\n" * 100)
    result = ReadFileTool(tmp_path, max_output_chars=10).execute({"reads": [{"path": "a"}]})
    data = result.data["results"][0]["data"]
    assert data["content"] == "1: x"
    assert data["next_start_line"] == 2 and data["total_lines"] == 201


@pytest.mark.parametrize("limit", [1, 7, 1000])
def test_smallest_items_matches_stable_sort_and_retains_only_limit(limit):
    items = [{"key": i % 11, "id": i} for i in range(500)]
    random.Random(17).shuffle(items)
    selected = SmallestItems(limit, key=lambda item: item["key"])
    for item in items:
        selected.add(item)
        assert len(selected._heap) <= limit
    assert selected.total == len(items)
    assert selected.sorted_items() == sorted(items, key=lambda item: item["key"])[:limit]


def test_directory_top_k_preserves_type_casefold_and_name_order(tmp_path):
    for name in ["z", "b", "A", "a", "ß", "SS", "ss", "Ω"]:
        (tmp_path / name).write_text("content")
    for name in ["last-directory", "First-directory"]:
        (tmp_path / name).mkdir()
    full = ListFileTool(tmp_path, max_entries=100).execute({"path": "."})
    limited = ListFileTool(tmp_path, max_entries=4).execute({"path": "."})
    assert full.success and limited.success
    assert limited.data["entries"] == full.data["entries"][:4]
    assert limited.data["total_entries"] == full.data["total_entries"]
    assert limited.data["truncated"]
