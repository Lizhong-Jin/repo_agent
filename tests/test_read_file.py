import hashlib
import json

import httpx
import pytest

from llm import LLMClient, LLMConfig, LLMRequest, Message
from tools import ReadFileTool


def test_read_range_and_line_numbers(tmp_path):
    original_bytes = "\ufeff第一行\r\n第二行\r\n第三行\r\n".encode("utf-8")
    (tmp_path / "代码.py").write_bytes(original_bytes)
    reader = ReadFileTool(tmp_path)
    result = reader.execute({"path": "代码.py", "start_line": 2, "end_line": 99})
    assert result.success
    assert result.data == {
        "path": "代码.py",
        "content": "2: 第二行\n3: 第三行",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "truncated": False,
        "next_start_line": None,
        "sha256": hashlib.sha256(original_bytes).hexdigest(),
    }


def test_pagination_and_explicit_end(tmp_path):
    (tmp_path / "a").write_text("a\nb\nc\nd\ne")
    reader = ReadFileTool(tmp_path, max_lines=2)
    first = reader.execute({"path": "a"})
    assert first.data["content"] == "1: a\n2: b"
    assert first.data["truncated"]
    second = reader.execute({"path": "a", "start_line": first.data["next_start_line"]})
    assert second.data["content"] == "3: c\n4: d"
    explicit = reader.execute({"path": "a", "end_line": 2})
    assert not explicit.data["truncated"]
    assert explicit.data["next_start_line"] is None


@pytest.mark.parametrize(
    "text,total,content",
    [
        ("", 0, ""),
        ("\n", 1, "1: "),
        ("a\n\n", 2, "1: a\n2: "),
        ("a", 1, "1: a"),
        ("a\u2028b", 1, "1: a\u2028b"),
    ],
)
def test_empty_and_line_boundaries(tmp_path, text, total, content):
    (tmp_path / "a").write_text(text)
    result = ReadFileTool(tmp_path).execute({"path": "a"})
    assert result.success
    assert result.data["total_lines"] == total
    assert result.data["content"] == content
    if not total:
        assert result.data["start_line"] is None
        assert result.data["end_line"] is None


def test_path_boundaries_and_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (root / "local.txt").write_text("local")
    (root / "external-link").symlink_to(outside)
    (root / "local-link").symlink_to(root / "local.txt")
    reader = ReadFileTool(root)
    for path in ("../outside.txt", str(outside), "external-link"):
        result = reader.execute({"path": path})
        assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
        assert "outside" not in result.data
    assert reader.execute({"path": str(root / "local.txt")}).success
    assert reader.execute({"path": "local-link"}).data["content"] == "1: local"


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"path": ""},
        {"path": "\x00"},
        {"path": 1},
        {"path": "a", "extra": 1},
        {"path": "a", "start_line": 0},
        {"path": "a", "start_line": True},
        {"path": "a", "end_line": None},
        {"path": "a", "end_line": 1.5},
        {"path": "a", "start_line": 3, "end_line": 2},
    ],
)
def test_invalid_arguments(tmp_path, arguments):
    result = ReadFileTool(tmp_path).execute(arguments)
    assert not result.success
    assert result.error_code == "INVALID_ARGUMENTS"


def test_filesystem_errors_and_size_limits(tmp_path):
    reader = ReadFileTool(tmp_path, max_file_bytes=10, max_output_chars=5)
    assert reader.execute({"path": "missing"}).error_code == "FILE_NOT_FOUND"
    assert reader.execute({"path": "."}).error_code == "NOT_A_FILE"
    (tmp_path / "a").write_bytes(b"\x00")
    assert reader.execute({"path": "a"}).error_code == "BINARY_FILE"
    (tmp_path / "a").write_bytes(b"\xff")
    assert reader.execute({"path": "a"}).error_code == "UNSUPPORTED_ENCODING"
    (tmp_path / "a").write_text("a" * 11)
    assert reader.execute({"path": "a"}).error_code == "FILE_TOO_LARGE"
    (tmp_path / "a").write_text("abcd")
    assert reader.execute({"path": "a"}).error_code == "OUTPUT_TOO_LARGE"
    assert reader.execute({"path": "a", "start_line": 2}).error_code == "LINE_OUT_OF_RANGE"


def test_root_is_fixed_at_construction(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("source")
    reader = ReadFileTool(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert reader.execute({"path": "a"}).data["content"] == "1: source"


@pytest.mark.parametrize("path,expected_error", [("source.py", False), ("missing.py", True)])
def test_model_tool_result_round_trip(tmp_path, path, expected_error):
    (tmp_path / "source.py").write_text("print('hello')\n")
    reader = ReadFileTool(tmp_path)
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["tools"][0]["function"]["name"] == "read_file"
        if len(sent) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert body["messages"][-1]["tool_call_id"] == "read_1"
            result = json.loads(body["messages"][-1]["content"])
            if expected_error:
                result = json.loads(result["error"])
                assert result["error"]["code"] == "FILE_NOT_FOUND"
            else:
                assert result["data"]["content"] == "1: print('hello')"
            message = {"role": "assistant", "content": "done"}
            finish = "stop"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("deepseek", "test-model", api_key="mock"), http_client=http)
        history = [Message("user", "read file")]
        first = client.generate(LLMRequest(history, tools=[reader.definition]))
        call = first.tool_calls[0]
        tool_message = reader.execute(call.arguments).to_message(call)
        assert tool_message.is_error == expected_error
        history.extend([first.to_message(), tool_message])
        assert client.generate(LLMRequest(history, tools=[reader.definition])).text == "done"
