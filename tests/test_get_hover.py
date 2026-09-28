"""Hover output budgets include JSON escaping, structure and metadata."""

import copy
import hashlib
import json

import pytest

from tools.semantic import GetHoverTool

SPAN = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}


class Peer:
    capabilities = {"hoverProvider": True}

    def __init__(self, result):
        self.result = result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def sync_document(self, path, *, text):
        return path.as_uri()

    def request(self, method, params):
        assert method == "textDocument/hover"
        return copy.deepcopy(self.result)


@pytest.fixture
def query(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")

    def run(contents, *, budget=20_000, text_limit=12_000, null=False):
        tool = GetHoverTool(
            tmp_path,
            execution_allowed=True,
            max_output_chars=budget,
            max_hover_chars=text_limit,
        )
        peer = Peer(None if null else {"contents": contents, "range": SPAN})
        monkeypatch.setattr(tool, "_new_client", lambda _: peer)
        return tool.execute({"path": "a.py", "line": 1, "column": 1})

    return run


def size(data):
    return len(json.dumps(data, ensure_ascii=False))


@pytest.mark.parametrize(
    "contents",
    [
        ["x"] * 600,
        "\n" * 12_000,
        "\x00" * 12_000,
        '\\"\t🙂中文' * 3_000,
        [""] * 2_000,
        {"language": "x" * 25_000, "value": "x"},
    ],
    ids=["short-parts", "newlines", "control-chars", "unicode", "empty-parts", "language"],
)
def test_oversized_responses_keep_a_bounded_prefix(query, contents):
    result = query(contents)
    assert result.success
    assert size(result.data) <= 20_000
    hover = result.data["hover"]
    assert hover["content_truncated"]
    assert hover["returned_content_chars"] == sum(len(p["value"]) for p in hover["contents"])
    assert hover["returned_content_chars"] <= min(hover["content_chars"], 12_000)
    original = GetHoverTool._normalize_hover_contents(contents)
    assert hover["content_chars"] == sum(len(p["value"]) for p in original)
    for index, part in enumerate(hover["contents"]):
        assert original[index]["value"].startswith(part["value"])
        if part["value"] != original[index]["value"]:
            assert part["truncated"] and index == len(hover["contents"]) - 1
    assert hover["range"] == {"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 2}}
    assert result.data["sha256"] == hashlib.sha256(b"x = 1\n").hexdigest()


def test_short_parts_retain_information(query):
    result = query(["x"] * 600)
    assert result.success
    parts = result.data["hover"]["contents"]
    assert 0 < len(parts) < 600
    assert all(p == {"kind": "markdown", "value": "x"} for p in parts)


def test_preserves_complete_parts_before_clipping_the_next(query):
    contents = [{"language": "python", "value": "x: int"}, "\n" * 12_000, "tail"]
    result = query(contents)
    assert result.success
    hover = result.data["hover"]
    assert len(hover["contents"]) == 2
    assert hover["contents"][0] == {"kind": "code", "language": "python", "value": "x: int"}
    assert hover["contents"][1]["truncated"]
    assert 0 < len(hover["contents"][1]["value"]) < 12_000
    # An extra escaped newline would exceed the budget, including counter digits.
    larger = copy.deepcopy(result.data)
    larger["hover"]["contents"][1]["value"] += "\n"
    larger["hover"]["returned_content_chars"] += 1
    assert size(larger) > 20_000


def test_exact_budget_leaves_response_unchanged(query):
    original = query(["hello", {"language": "python", "value": "x: int"}])
    result = query(["hello", {"language": "python", "value": "x: int"}], budget=size(original.data))
    assert result.success and result.data == original.data
    assert not result.data["hover"]["content_truncated"]


def test_text_limit_remains_independent(query):
    result = query("abcdefghij", text_limit=3)
    assert result.success
    assert result.data["hover"]["contents"] == [
        {"kind": "markdown", "value": "abc", "truncated": True}
    ]
    assert result.data["hover"]["content_chars"] == 10
    assert result.data["hover"]["returned_content_chars"] == 3
    assert result.data["hover"]["content_truncated"]


def test_minimum_metadata_budget_and_one_character_less(query):
    full = query("hello")
    minimum = copy.deepcopy(full.data)
    minimum["hover"].update(contents=[], returned_content_chars=0, content_truncated=True)
    result = query("hello", budget=size(minimum))
    assert result.success and result.data == minimum
    assert query("hello", budget=size(minimum) - 1).error_code == "OUTPUT_TOO_LARGE"


@pytest.mark.parametrize("null", [False, True])
def test_empty_or_null_hover_budget(query, null):
    original = query([], null=null)
    exact = query([], budget=size(original.data), null=null)
    assert exact.success and exact.data == original.data
    assert query([], budget=size(original.data) - 1, null=null).error_code == "OUTPUT_TOO_LARGE"
