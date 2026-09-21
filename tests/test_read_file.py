import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from llm import LLMClient, LLMConfig, LLMRequest, Message
from tools import ReadFileTool


def read_one(reader, **request):
    return reader.execute({"reads": [request]}).data["results"][0]


def test_multiple_files_independent_ranges_and_hashes(tmp_path):
    original_bytes = "\ufeff第一行\r\n第二行\r\n第三行\r\n".encode("utf-8")
    (tmp_path / "代码.py").write_bytes(original_bytes)
    (tmp_path / "other.py").write_text("first\nsecond\nthird")
    arguments = {"reads": [
        {"path": "代码.py", "start_line": 2, "end_line": 99},
        {"path": "other.py", "end_line": 1},
        {"path": "代码.py", "end_line": 1},
    ]}
    before = deepcopy(arguments)
    result = ReadFileTool(tmp_path).execute(arguments)
    assert result.success
    assert arguments == before
    entries = result.data["results"]
    assert [entry["path"] for entry in entries] == ["代码.py", "other.py", "代码.py"]
    assert entries[0] == {
        "path": "代码.py", "success": True,
        "data": {
            "path": "代码.py", "content": "2: 第二行\n3: 第三行",
            "start_line": 2, "end_line": 3, "total_lines": 3,
            "truncated": False, "next_start_line": None,
            "sha256": hashlib.sha256(original_bytes).hexdigest(),
        },
    }
    assert entries[1]["data"]["content"] == "1: first"
    assert entries[2]["data"]["content"] == "1: 第一行"
    assert result.data["total_output_chars"] == sum(len(e["data"]["content"]) for e in entries)


def test_shared_budget_and_lossless_pagination(tmp_path):
    (tmp_path / "a").write_text("a\nb\nc\nd\ne")
    (tmp_path / "b").write_text("x\ny\nz")
    reader = ReadFileTool(tmp_path, max_output_chars=14)
    result = reader.execute({"reads": [{"path": "a", "end_line": 1}, {"path": "b"}]})
    first, second = result.data["results"]
    assert first["data"]["content"] == "1: a"
    assert not first["data"]["truncated"]
    assert second["data"]["content"] == "1: x\n2: y"
    assert second["data"]["truncated"]
    assert second["data"]["next_start_line"] == 3
    continuation = read_one(reader, path="b", start_line=3)
    assert continuation["data"]["content"] == "3: z"
    assert not continuation["data"]["truncated"]
    assert result.data["total_output_chars"] == 13


def test_exhausted_budget_and_oversized_line_do_not_hide_other_results(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "large").write_text("x" * 10)
    (tmp_path / "empty").write_text("")
    result = ReadFileTool(tmp_path, max_output_chars=4).execute({"reads": [
        {"path": "large"}, {"path": "a"}, {"path": "a"}, {"path": "empty"},
    ]})
    entries = result.data["results"]
    assert not result.success
    assert result.error_code == "READ_FAILED"
    assert entries[0]["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert entries[1]["data"]["content"] == "1: a"
    assert entries[2]["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert entries[3]["success"] and entries[3]["data"]["content"] == ""
    assert result.data["total_output_chars"] == 4


def test_long_line_after_prefix_resumes_without_skipping(tmp_path):
    (tmp_path / "a").write_text("x\nlong line\nz")
    result = read_one(ReadFileTool(tmp_path, max_output_chars=8), path="a")
    assert result["data"]["content"] == "1: x"
    assert result["data"]["next_start_line"] == 2
    retry = read_one(ReadFileTool(tmp_path, max_output_chars=32), path="a", start_line=2)
    assert retry["data"]["content"] == "2: long line\n3: z"


@pytest.mark.parametrize("text,total,content", [
    ("", 0, ""), ("\n", 1, "1: "), ("a\n\n", 2, "1: a\n2: "),
    ("a", 1, "1: a"), ("a\u2028b", 1, "1: a\u2028b"),
    ("a\rb\r", 2, "1: a\n2: b"),
])
def test_empty_and_line_boundaries(tmp_path, text, total, content):
    (tmp_path / "a").write_bytes(text.encode())
    entry = read_one(ReadFileTool(tmp_path), path="a")
    assert entry["success"]
    assert entry["data"]["total_lines"] == total
    assert entry["data"]["content"] == content
    if not total:
        assert entry["data"]["start_line"] is None
        assert entry["data"]["end_line"] is None


def test_mixed_failures_path_boundaries_and_symlinks(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE_CONTENT")
    (root / "local.txt").write_text("local")
    (root / ".env").write_text("SECRET_CONTENT")
    (root / "external-link").symlink_to(outside)
    (root / "local-link").symlink_to(root / "local.txt")
    reader = ReadFileTool(root)
    paths = ["../outside.txt", str(outside), "external-link", ".env", "missing", "local-link", str(root / "local.txt")]
    result = reader.execute({"reads": [{"path": p} for p in paths]})
    assert not result.success
    entries = result.data["results"]
    assert [e["error"]["code"] for e in entries[:5]] == [
        "PATH_OUTSIDE_WORKSPACE", "PATH_OUTSIDE_WORKSPACE", "PATH_OUTSIDE_WORKSPACE", "PROTECTED_FILE", "FILE_NOT_FOUND",
    ]
    assert entries[5]["data"]["content"] == entries[6]["data"]["content"] == "1: local"
    assert "OUTSIDE_CONTENT" not in str(result)
    assert "SECRET_CONTENT" not in str(result)


@pytest.mark.parametrize("arguments", [
    None, {}, {"path": "a"}, {"reads": None}, {"reads": {}}, {"reads": []},
    {"reads": [{"path": "a"}], "extra": 1},
    *({"reads": [entry]} for entry in [
        None, "a", {}, {"path": ""}, {"path": "  "}, {"path": "\x00"}, {"path": 1},
        {"path": "a", "extra": 1}, {"path": "a", "start_line": 0},
        {"path": "a", "start_line": True}, {"path": "a", "end_line": None},
        {"path": "a", "end_line": True}, {"path": "a", "end_line": 1.5},
        {"path": "a", "start_line": 3, "end_line": 2},
    ]),
])
def test_invalid_arguments(tmp_path, arguments):
    result = ReadFileTool(tmp_path).execute(arguments)
    assert not result.success
    assert result.error_code == "INVALID_ARGUMENTS"


def test_validation_precedes_reads_and_enforces_batch_limit(tmp_path, monkeypatch):
    reader = ReadFileTool(tmp_path, max_reads=2)
    def forbidden(*args):
        pytest.fail("Malformed requests must not read files")
    monkeypatch.setattr(reader, "_read_one", forbidden)
    for reads in [[{"path": "a"}, {}], [{"path": "a"}] * 3]:
        assert reader.execute({"reads": reads}).error_code == "INVALID_ARGUMENTS"
    schema = reader.definition.parameters
    assert schema["required"] == ["reads"]
    assert schema["properties"]["reads"]["maxItems"] == 2


@pytest.mark.parametrize("option", ["max_reads", "max_file_bytes", "max_output_chars"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_limits(tmp_path, option, value):
    with pytest.raises(ValueError):
        ReadFileTool(tmp_path, **{option: value})


def test_filesystem_errors_and_size_limits(tmp_path):
    reader = ReadFileTool(tmp_path, max_file_bytes=10, max_output_chars=5)
    assert read_one(reader, path="missing")["error"]["code"] == "FILE_NOT_FOUND"
    assert read_one(reader, path=".")["error"]["code"] == "NOT_A_FILE"
    for content, error in [(b"\x00", "BINARY_FILE"), (b"\xff", "UNSUPPORTED_ENCODING"), (b"a" * 11, "FILE_TOO_LARGE"), (b"abcd", "OUTPUT_TOO_LARGE")]:
        (tmp_path / "a").write_bytes(content)
        assert read_one(reader, path="a")["error"]["code"] == error
    assert read_one(reader, path="a", start_line=2)["error"]["code"] == "LINE_OUT_OF_RANGE"


def test_root_is_fixed_at_construction(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("source")
    reader = ReadFileTool(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert read_one(reader, path="a")["data"]["content"] == "1: source"


@pytest.mark.parametrize("include_missing", [False, True])
def test_model_tool_result_round_trip(tmp_path, include_missing):
    (tmp_path / "source.py").write_text("print('hello')\n")
    reader = ReadFileTool(tmp_path)
    reads = [{"path": "source.py"}]
    if include_missing:
        reads.insert(0, {"path": "missing.py"})
    sent = []
    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        assert body["tools"][0]["function"]["parameters"]["required"] == ["reads"]
        if len(sent) == 1:
            message = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "read_1", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"reads": reads}),
                },
            }]}
            finish = "tool_calls"
        else:
            assert body["messages"][-1]["tool_call_id"] == "read_1"
            result = json.loads(body["messages"][-1]["content"])
            if include_missing:
                result = json.loads(result["error"])
                assert result["error"]["code"] == "READ_FAILED"
                assert result["data"]["results"][0]["error"]["code"] == "FILE_NOT_FOUND"
            assert result["data"]["results"][-1]["data"]["content"] == "1: print('hello')"
            message = {"role": "assistant", "content": "done"}
            finish = "stop"
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("deepseek", "test-model", api_key="mock"), http_client=http)
        history = [Message("user", "read files")]
        first = client.generate(LLMRequest(history, tools=[reader.definition]))
        call = first.tool_calls[0]
        tool_message = reader.execute(call.arguments).to_message(call)
        assert tool_message.is_error == include_missing
        history.extend([first.to_message(), tool_message])
        assert client.generate(LLMRequest(history, tools=[reader.definition])).text == "done"
