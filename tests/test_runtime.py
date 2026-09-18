import json
from copy import deepcopy

import httpx
import pytest

from agent import AgentRuntime
from llm import (
    InvalidResponseError,
    LLMClient,
    LLMConfig,
    LLMResponse,
    LLMTimeoutError,
    Message,
    ToolCall,
    ToolDefinition,
)
from tools import ReadFileTool, ToolResult


def reply(text="", calls=(), finish=None):
    return LLMResponse(
        provider="test",
        model="test",
        message=Message("assistant", text, calls),
        finish_reason=finish or ("tool_calls" if calls else "stop"),
    )


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(deepcopy(request))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class RecordingTool:
    definition = ToolDefinition("record", "Record a value")

    def __init__(self):
        self.seen = []

    def execute(self, arguments):
        self.seen.append(deepcopy(arguments))
        arguments.clear()  # Runtime must isolate the model's original arguments.
        return ToolResult(True, {"recorded": True})


def test_direct_answer_and_independent_runs():
    model = ScriptedLLM([reply("first"), reply("second")])
    runtime = AgentRuntime(model, system_prompt="custom", max_output_tokens=123)
    first = runtime.run("question1")
    assert first.status == "completed"
    assert first.text == "first"
    assert first.steps == 1
    assert [m.role for m in first.history] == ["system", "user", "assistant"]
    assert model.requests[0].messages[0].content == "custom"
    assert model.requests[0].max_output_tokens == 123
    runtime.run("question2")
    assert len(model.requests[1].messages) == 2
    assert model.requests[1].messages[-1].content == "question2"


def test_multiple_calls_in_order_and_no_argument_mutation():
    calls = [ToolCall("a", "record", {"value": 1}), ToolCall("b", "record", {"value": 2})]
    model = ScriptedLLM([reply(calls=calls), reply("done")])
    tool = RecordingTool()
    result = AgentRuntime(model, [tool]).run("record values")
    assert result.status == "completed"
    assert result.steps == 2
    assert tool.seen == [{"value": 1}, {"value": 2}]
    assert [m.tool_call_id for m in model.requests[1].messages if m.role == "tool"] == ["a", "b"]
    assert calls[0].arguments == {"value": 1}
    assert model.requests[0].tools[0].name == "record"


def test_unknown_tool_is_returned_to_model():
    model = ScriptedLLM([reply(calls=[ToolCall("a", "unknown", {})]), reply("cannot use it")])
    result = AgentRuntime(model).run("task")
    error = model.requests[1].messages[-1]
    assert error.is_error
    assert json.loads(error.content)["error"]["code"] == "UNKNOWN_TOOL"
    assert result.status == "completed"


def test_file_error_then_model_corrects_path(tmp_path):
    (tmp_path / "actual.txt").write_text("actual content")
    model = ScriptedLLM(
        [
            reply(calls=[ToolCall("a", "read_file", {"path": "missing.txt"})]),
            reply(calls=[ToolCall("b", "read_file", {"path": "actual.txt"})]),
            reply("found actual content"),
        ]
    )
    result = AgentRuntime(model, [ReadFileTool(tmp_path)]).run("read actual.txt")
    assert model.requests[1].messages[-1].is_error
    assert not model.requests[2].messages[-1].is_error
    assert "actual content" in model.requests[2].messages[-1].content
    assert result.status == "completed"
    assert result.steps == 3


@pytest.mark.parametrize("invalid_result", [False, True])
def test_tool_exception_or_invalid_result_is_observation(invalid_result):
    class BrokenTool(RecordingTool):
        def execute(self, arguments):
            if invalid_result:
                return "not a ToolResult"
            raise RuntimeError("private exception details")

    model = ScriptedLLM([reply(calls=[ToolCall("a", "record", {})]), reply("tool failed")])
    result = AgentRuntime(model, [BrokenTool()]).run("task")
    assert result.status == "completed"
    observation = model.requests[1].messages[-1]
    assert observation.is_error
    assert json.loads(observation.content)["error"]["code"] == "TOOL_EXECUTION_ERROR"
    assert "private exception details" not in observation.content


@pytest.mark.parametrize("finish", ["length", "blocked", "other"])
def test_non_normal_finish_never_executes_tools(finish):
    model = ScriptedLLM([reply("partial", [ToolCall("a", "record", {})], finish)])
    tool = RecordingTool()
    result = AgentRuntime(model, [tool]).run("task")
    assert result.status == "stopped"
    assert result.response.finish_reason == finish
    assert result.text == "partial"
    assert not tool.seen


def test_iteration_limit_keeps_last_tool_results():
    model = ScriptedLLM(
        [
            reply(calls=[ToolCall("a", "record", {"i": 1})]),
            reply(calls=[ToolCall("b", "record", {"i": 2})]),
        ]
    )
    tool = RecordingTool()
    result = AgentRuntime(model, [tool], max_steps=2).run("keep going")
    assert result.status == "max_steps"
    assert result.steps == len(model.requests) == 2
    assert len(tool.seen) == 2
    assert result.history[-1].tool_call_id == "b"


def test_model_errors_propagate():
    model = ScriptedLLM([LLMTimeoutError("timeout")])
    with pytest.raises(LLMTimeoutError):
        AgentRuntime(model).run("task")
    assert len(model.requests) == 1


def test_reused_call_ids_rejected_before_reexecuting():
    call = ToolCall("same", "record", {})
    model = ScriptedLLM([reply(calls=[call]), reply(calls=[call])])
    tool = RecordingTool()
    with pytest.raises(InvalidResponseError):
        AgentRuntime(model, [tool]).run("task")
    assert len(tool.seen) == 1


def test_bad_config_and_empty_task():
    model = ScriptedLLM([])
    for steps in (0, -1, True, 1.2):
        with pytest.raises(ValueError):
            AgentRuntime(model, max_steps=steps)
    with pytest.raises(ValueError, match="Duplicate"):
        AgentRuntime(model, [RecordingTool(), RecordingTool()])
    with pytest.raises(ValueError):
        AgentRuntime(model).run(" ")
    assert not model.requests


def test_real_client_and_read_file_with_mock_http(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')\n")
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "provider-state",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assert body["messages"][-2]["reasoning_content"] == "provider-state"
            result = json.loads(body["messages"][-1]["content"])
            assert result["data"]["content"] == "1: print('hello')"
            message = {"role": "assistant", "content": "代码打印 hello。"}
            finish = "stop"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = LLMClient(LLMConfig("deepseek", "test", api_key="mock"), http_client=transport)
        result = AgentRuntime(client, [ReadFileTool(tmp_path)]).run("解释 main.py")
    assert result.status == "completed"
    assert result.text == "代码打印 hello。"


def test_cli_uses_runtime(monkeypatch, tmp_path, capsys):
    from cli import main as cli

    class FakeClient(ScriptedLLM):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    model = FakeClient([reply("CLI result")])
    monkeypatch.setattr(cli, "LLMClient", lambda config: model)
    monkeypatch.setattr(
        "sys.argv", ["repo-agent", "--sandbox", "local", "task", "--model", "test", "--root", str(tmp_path)]
    )
    cli.main()
    output = capsys.readouterr().out
    assert "CLI result\n" in output
    assert "思考设置：auto" in output
    assert model.requests[0].messages[-1].content == "task"
