import json
from copy import deepcopy

import pytest

from agent import AgentRuntime
from cli.interactive import run_interactive
from llm import (
    InvalidRequestError,
    InvalidResponseError,
    LLMResponse,
    LLMTimeoutError,
    Message,
    ToolCall,
)
from llm.adapters.chat_completions import ChatCompletionsAdapter
from tools import ReadFileTool, WriteFileTool


def reply(text="done", *, calls=(), finish=None):
    return LLMResponse(
        provider="test",
        model="test",
        message=Message("assistant", text, calls),
        finish_reason=finish or ("tool_calls" if calls else "stop"),
    )


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def generate(self, request):
        self.requests.append(deepcopy(request))
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        return result


def inputs(monkeypatch, values):
    script = iter(values)

    def read(prompt):
        value = next(script)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", read)


def test_continuation_keeps_provider_state_without_mutating_input():
    adapter = ChatCompletionsAdapter("deepseek", "model")
    native_response = adapter.decode(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "first",
                        "reasoning_content": "state",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    model = Model([native_response, reply("second")])
    runtime = AgentRuntime(model)
    first = runtime.run("task1")
    snapshot = deepcopy(first.history)
    second = runtime.run("task2", history=first.history)
    assert first.history == snapshot
    assert second.steps == 1
    assert [m.content for m in model.requests[1].messages[1:]] == ["task1", "first", "task2"]
    wire = adapter.encode(model.requests[1])
    assert wire["messages"][-2]["reasoning_content"] == "state"
    assert sum(m.role == "system" for m in second.history) == 1


def test_reused_tool_id_in_next_task_is_rejected(tmp_path):
    (tmp_path / "a").write_text("a")
    call = ToolCall("same", "read_file", {"reads": [{"path": "a"}]})
    runtime = AgentRuntime(
        Model([reply(calls=[call]), reply(), reply(calls=[call])]), [ReadFileTool(tmp_path)]
    )
    first = runtime.run("read a")
    with pytest.raises(InvalidResponseError):
        runtime.run("read again", history=first.history)


def test_incomplete_supplied_history_rejected_before_model_call():
    model = Model([])
    history = [Message("user", "x"), Message("assistant", tool_calls=[ToolCall("a", "x", {})])]
    with pytest.raises(InvalidRequestError):
        AgentRuntime(model).run("next", history=history)
    assert not model.requests


def test_continuous_tasks_share_read_and_write_results(tmp_path, monkeypatch):
    (tmp_path / "source.txt").write_text("source")
    model = Model(
        [
            reply(calls=[ToolCall("read", "read_file", {"reads": [{"path": "source.txt"}]})]),
            reply("read source"),
            reply(
                calls=[
                    ToolCall("write", "write_file", {"path": "summary.txt", "content": "summary"})
                ]
            ),
            reply("wrote summary"),
        ]
    )
    inputs(monkeypatch, ["read source", "write summary from that", "/exit"])
    run_interactive(AgentRuntime(model, [ReadFileTool(tmp_path), WriteFileTool(tmp_path)]))
    second_task = model.requests[2].messages
    assert second_task[-1].content == "write summary from that"
    tool_result = next(m for m in second_task if m.role == "tool")
    assert json.loads(tool_result.content)["data"]["results"][0]["data"]["content"] == "1: source"
    assert (tmp_path / "summary.txt").read_text() == "summary"


def test_clear_drops_context_but_not_written_files(tmp_path, monkeypatch, capsys):
    model = Model(
        [
            reply(calls=[ToolCall("a", "write_file", {"path": "a", "content": "kept"})]),
            reply(),
            reply(),
        ]
    )
    inputs(monkeypatch, ["write", "/clear", "new question", "/quit"])
    run_interactive(AgentRuntime(model, [WriteFileTool(tmp_path)]))
    assert [m.role for m in model.requests[-1].messages] == ["system", "user"]
    assert (tmp_path / "a").read_text() == "kept"
    assert "上下文已清空" in capsys.readouterr().out


def test_meta_commands_do_not_call_model(monkeypatch, capsys):
    inputs(monkeypatch, ["", "  ", "/help", "/unknown", KeyboardInterrupt(), "/exit"])
    model = Model([])
    run_interactive(AgentRuntime(model))
    assert not model.requests
    output = capsys.readouterr().out
    assert "未知会话命令" in output
    assert "输入已取消" in output


def test_eof_exits(monkeypatch, capsys):
    inputs(monkeypatch, [EOFError()])
    run_interactive(AgentRuntime(Model([])))
    assert "会话已结束" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [LLMTimeoutError("timeout"), KeyboardInterrupt()])
def test_error_or_interrupt_after_write_resets_context_and_accepts_next_task(
    tmp_path, monkeypatch, capsys, failure
):
    model = Model(
        [
            reply(calls=[ToolCall("a", "write_file", {"path": "a", "content": "kept"})]),
            failure,
            reply("recovered"),
        ]
    )
    inputs(monkeypatch, ["write file", "next task", "/exit"])
    run_interactive(AgentRuntime(model, [WriteFileTool(tmp_path)]))
    assert (tmp_path / "a").read_text() == "kept"
    assert [m.role for m in model.requests[-1].messages] == ["system", "user"]
    output = capsys.readouterr().out
    assert "recovered" in output
    assert "文件操作不会撤销" in output


@pytest.mark.parametrize("finish", ["length", "blocked", "other"])
def test_stopped_reply_does_not_poison_next_task(monkeypatch, finish):
    model = Model([reply(calls=[ToolCall("partial", "x", {})], finish=finish), reply()])
    inputs(monkeypatch, ["first", "second", "/exit"])
    run_interactive(AgentRuntime(model, max_recoveries=0))
    if finish == "length":
        assert any(m.content == "first" for m in model.requests[-1].messages)
        assert not any(m.tool_calls for m in model.requests[-1].messages)
        model.requests[-1].validate()
    else:
        assert [m.role for m in model.requests[-1].messages] == ["system", "user"]


def test_continue_after_step_limit_has_a_fresh_budget(tmp_path, monkeypatch, capsys):
    (tmp_path / "a").write_text("a")
    model = Model([reply(calls=[ToolCall("read", "read_file", {"reads": [{"path": "a"}]})]), reply("finished")])
    inputs(monkeypatch, ["read", "continue", "/exit"])
    run_interactive(AgentRuntime(model, [ReadFileTool(tmp_path)], max_steps=1))
    assert model.requests[1].messages[-2].role == "tool"
    assert model.requests[1].messages[-1].content == "continue"
    output = capsys.readouterr().out
    assert "达到轮数上限" in output
    assert "模型调用轮数" not in output


def test_main_without_task_starts_session_and_closes_client(tmp_path, monkeypatch, capsys):
    from cli import main as cli

    class Client(Model):
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    client = Client([reply("answer1"), reply("answer2")])
    monkeypatch.setattr(cli, "LLMClient", lambda config: client)
    monkeypatch.setattr(
        "sys.argv", ["repo-agent", "--sandbox", "local", "--model", "test", "--root", str(tmp_path)]
    )
    inputs(monkeypatch, ["one", "two", "/exit"])
    cli.main()
    assert len(client.requests) == 2
    assert client.closed
    assert "answer2" in capsys.readouterr().out
