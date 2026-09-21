import hashlib

import pytest

from tools import EditFileTool


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize(
    ("before", "old_text", "new_text", "after", "start_line"),
    [
        ("a\r\nb\nc\r", "b", "B", "a\r\nB\nc\r", 2),
        ("a\r\nb\r\nc", "b", "B", "a\r\nB\r\nc", 2),
        ("a\rb\nc", "b", "B", "a\rB\nc", 2),
        ("前\r\n中\n后\r\n尾", "中\n后", "新", "前\r\n新\r\n尾", 2),
        ("a\r\nb\nc", "\nb", "", "a\nc", 1),
        ("a\r\nb\nc", "a", "A", "A\r\nb\nc", 1),
        ("a\r\nb\nc", "c", "C", "a\r\nb\nC", 3),
        ("a\r\nb\nc", "a\nb\nc", "X", "X", 1),
        ("a\r\nb\nc", "a\r\nb", "X", "X\nc", 1),
        # A match without newlines uses LF, preserving all surrounding bytes.
        ("a\r\nb\nc", "b", "B\nD", "a\r\nB\nD\nc", 2),
        ("a\rb\rc", "b", "B\nD", "a\rB\nD\rc", 2),
        # A match containing newlines preserves that fragment's convention.
        ("a\r\nb\r\nc", "b\nc", "B\nD", "a\r\nB\r\nD", 2),
        ("a\rb\rc", "b\nc", "B\nD", "a\rB\rD", 2),
        ("a\nb\nc", "b", "B\nD", "a\nB\nD\nc", 2),
        ("a\r\nb\nc", "a\nb\nc", "", "", 1),
    ],
)
def test_edit_preserves_unmatched_bytes_and_bom(
    tmp_path, bom, before, old_text, new_text, after, start_line
):
    target = tmp_path / "source.txt"
    original = bom + before.encode("utf-8")
    expected = bom + after.encode("utf-8")
    target.write_bytes(original)

    result = EditFileTool(tmp_path).execute(
        {"path": target.name, "edits": [{"old_text": old_text, "new_text": new_text}]}
    )

    assert result.success
    assert target.read_bytes() == expected
    assert result.data["edits_applied"] == 1
    assert result.data["changes"][0]["start_line"] == start_line
    assert result.data["bytes_before"] == len(original)
    assert result.data["bytes_after"] == len(expected)
    assert result.data["old_sha256"] == hashlib.sha256(original).hexdigest()
    assert result.data["new_sha256"] == hashlib.sha256(expected).hexdigest()
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize(
    ("before", "old_text"),
    [
        ("aaa", "aa"),
        ("ababa", "aba"),
        ("哈哈哈", "哈哈"),
        ("a\r\na\na", "a\na"),
        ("a\r\na\na", "a\r\na"),
        ("abc abc", "abc"),
    ],
)
def test_multiple_matches_including_overlaps_leave_file_unchanged(tmp_path, before, old_text):
    target = tmp_path / "source.txt"
    original = before.encode("utf-8")
    target.write_bytes(original)

    result = EditFileTool(tmp_path).execute(
        {"path": target.name, "edits": [{"old_text": old_text, "new_text": "X"}]}
    )

    assert not result.success
    assert result.error_code == "MULTIPLE_MATCHES"
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_preserved_bom_counts_toward_result_size_limit(tmp_path):
    target = tmp_path / "source.txt"
    original = b"\xef\xbb\xbfabc"
    target.write_bytes(original)

    result = EditFileTool(tmp_path, max_content_bytes=len(original)).execute(
        {"path": target.name, "edits": [{"old_text": "abc", "new_text": "abcd"}]}
    )

    assert result.error_code == "EDIT_RESULT_TOO_LARGE"
    assert target.read_bytes() == original


def test_no_match_leaves_file_unchanged(tmp_path):
    target = tmp_path / "source.txt"
    original = b"\xef\xbb\xbfa\r\nb\nc"
    target.write_bytes(original)

    result = EditFileTool(tmp_path).execute(
        {"path": target.name, "edits": [{"old_text": "missing", "new_text": "X"}]}
    )

    assert result.error_code == "NO_MATCH"
    assert target.read_bytes() == original
