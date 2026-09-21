"""Exercise actual request/history boundaries, not only the recovery counter."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from test_interactive import inputs
from test_llm import payload
from test_runtime import RecordingTool, ScriptedLLM, reply
from test_streaming import ANTHROPIC, chat, sse

from agent import AgentRuntime
from agent.Tracing import Tracer
from cli.interactive import display_result, run_interactive
from cli.live import LiveOutput, SessionStatus
from llm import (
    AsyncLLMClient,
    InvalidResponseError,
    LLMClient,
    LLMConfig,
    LLMRequest,
    LLMTimeoutError,
    Message,
    ToolCall,
    Usage,
)
from llm.adapters import ADAPTERS

FORMATS = [
    ("deepseek", "chat_completions"),
    ("openai", "responses"),
    ("anthropic", "anthropic"),
    ("gemini", "gemini"),
]


def limited_payload(fmt, calls=True, malformed=False):
    data = payload(fmt, calls=calls)
    if fmt == "chat_completions":
        data["choices"][0]["finish_reason"] = "length"
        if calls and malformed:
            data["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"path":'
    elif fmt == "responses":
        data.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        if calls and malformed:
            data["output"][-1]["arguments"] = '{"path":'
    elif fmt == "anthropic":
        data["stop_reason"] = "max_tokens"
        if calls and malformed:
            data["content"][-1]["input"] = '{"path":'
    else:
        data["candidates"][0]["finishReason"] = "MAX_TOKENS"
        if calls and malformed:
            data["candidates"][0]["content"]["parts"][-1]["functionCall"]["args"] = '{"path":'
    return data


@pytest.mark.parametrize("provider,fmt", FORMATS)
@pytest.mark.parametrize("malformed", [False, True])
def test_length_discards_all_tool_blocks_even_valid_json(provider, fmt, malformed):
    data = limited_payload(fmt, malformed=malformed)
    before = deepcopy(data)
    response = ADAPTERS[fmt](provider, "m").decode(data)
    assert response.finish_reason == "length" and response.truncated_tool_calls
    assert not response.tool_calls and response.message.provider_state is None
    assert response.usage.input_tokens == 20
    assert data == before == response.raw


@pytest.mark.parametrize("provider,fmt", FORMATS)
def test_text_truncation_retains_only_safe_text_for_replay(provider, fmt):
    adapter = ADAPTERS[fmt](provider, "m")
    response = adapter.decode(limited_payload(fmt, calls=False))
    assert response.text == "检查完成" and not response.truncated_tool_calls
    request = LLMRequest([Message("user", "start"), response.message, Message("user", "continue")])
    wire = json.dumps(adapter.encode(request))
    assert "opaque" not in wire and "signed" not in wire


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("provider,fmt", FORMATS)
def test_truncated_stream_returns_usage_instead_of_json_error(provider, fmt, asynchronous):
    data = limited_payload(fmt, malformed=True)
    if fmt == "chat_completions":
        events = [
            chat({"tool_calls": [{"index": 0, **data["choices"][0]["message"]["tool_calls"][0]}]}),
            chat({}, "length"),
            {"choices": [], "usage": data["usage"]},
            "[DONE]",
        ]
    elif fmt == "responses":
        events = [{"type": "response.incomplete", "response": data}]
    elif fmt == "anthropic":
        events = deepcopy(ANTHROPIC)
        # Leave the final argument fragment incomplete, but still close the block.
        events[7]["delta"]["partial_json"] = '"missing'
        events[-2]["delta"]["stop_reason"] = "max_tokens"
    else:
        events = [data]
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(events)
        )
    )
    config = LLMConfig(provider, "m", api_key="test")
    request = LLMRequest([Message("user", "start")])
    if asynchronous:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                return await AsyncLLMClient(config, http_client=http).generate(request)

        response = asyncio.run(run())
    else:
        with httpx.Client(transport=transport) as http:
            response = LLMClient(config, http_client=http).generate(request)
    assert response.finish_reason == "length" and response.truncated_tool_calls
    assert not response.tool_calls and response.usage.output_tokens is not None


def test_anthropic_invalid_json_without_length_still_fails():
    from llm.streaming import StreamAssembler

    events = deepcopy(ANTHROPIC)
    events[7]["delta"]["partial_json"] = '"missing'
    assembler = StreamAssembler("anthropic")
    for line in sse(events).decode().splitlines():
        assembler.feed(line)
    with pytest.raises(InvalidResponseError):
        assembler.finish()


def test_text_continues_twice_accounts_usage_and_keeps_separate_history(tmp_path, capsys):
    chunks = [
        replace(reply(t, finish=f), usage=Usage(10, 4))
        for t, f in [("可以加载到", "length"), ("共享内存，", "length"), ("再测试。", "stop")]
    ]
    model = ScriptedLLM(chunks)
    status = SessionStatus(tmp_path)
    with Tracer(tmp_path / "logs") as tracer:

        def event(name, stats):
            status(name, stats)
            tracer(name, stats)

        result = AgentRuntime(model, on_event=event).run("optimize")
    assert result.status == "completed" and result.steps == 3
    assert result.text == "可以加载到共享内存，再测试。"
    assert [m.content for m in result.history if m.role == "assistant"] == [c.text for c in chunks]
    assert status.totals == {"input_tokens": 30, "output_tokens": 12}
    assert len(result.stats.recoveries) == 2
    for request in model.requests:
        request.validate()
    display_result(result)
    assert capsys.readouterr().out.count(result.text) == 1
    records = [json.loads(line) for line in tracer.jsonl_path.read_text().splitlines()]
    assert len([r for r in records if r["event"] == "recovery"]) == 2


def test_streaming_continuation_is_not_printed_twice(capsys):
    class Streaming(ScriptedLLM):
        def generate_with_events(self, request, event):
            response = self.generate(request)
            event("first_text", "", 0.1)
            event("text", response.text, 0.1)
            event("end", "", 0.2)
            return response

    model = Streaming([reply("截断片段", finish="length"), reply("续写完成")])
    runtime = AgentRuntime(model)
    runtime.on_model_event = LiveOutput()
    result = runtime.run("task")
    display_result(result)
    output = capsys.readouterr().out
    assert output.count("截断片段") == output.count("续写完成") == 1


def test_tool_recovery_does_not_repeat_prior_operations_and_resets_limit():
    tool = RecordingTool()

    def call(i):
        return ToolCall(str(i), "record", {"value": i})

    model = ScriptedLLM(
        [
            reply(calls=[call(1)]),
            reply("preparing", calls=[call(2), call(3)], finish="length"),
            reply(calls=[call(2), call(3)], finish="length"),
            reply(calls=[call(2), call(3)]),
            reply("done"),
        ]
    )
    runtime = AgentRuntime(model, [tool], max_output_tokens=100, recovery_max_output_tokens=300)
    result = runtime.run("record")
    assert result.status == "completed"
    assert tool.seen == [{"value": 1}, {"value": 2}, {"value": 3}]
    assert [r.max_output_tokens for r in model.requests] == [100, 100, 200, 300, 100]
    assert runtime.max_output_tokens == 100
    for request in model.requests:
        request.validate()
    assert "均未执行" in model.requests[2].messages[-1].content


@pytest.mark.parametrize(
    "max_steps,max_recoveries,expected_calls,status",
    [
        (8, 2, 3, "stopped"),
        (2, 2, 2, "max_steps"),
        (8, 0, 1, "stopped"),
    ],
)
def test_bounded_recovery_preserves_context_and_manual_continue(
    max_steps,
    max_recoveries,
    expected_calls,
    status,
):
    model = ScriptedLLM(
        [
            *[reply(f"part{i}", finish="length") for i in range(expected_calls)],
            reply("finished"),
        ]
    )
    runtime = AgentRuntime(model, max_steps=max_steps, max_recoveries=max_recoveries)
    first = runtime.run("task")
    assert first.status == status and first.resumable
    assert first.steps == len(model.requests) == expected_calls
    assert "尚未完成" in first.notice and "继续" in first.notice
    snapshot = deepcopy(first.history)
    second = runtime.run("继续", history=first.history)
    assert second.status == "completed" and second.steps == 1
    assert first.history == snapshot
    assert model.requests[-1].messages[-1].content == "继续"
    assert any(m.content == "part0" for m in model.requests[-1].messages)


def test_mixed_text_and_tool_truncation_share_recovery_limit():
    tool = RecordingTool()
    model = ScriptedLLM(
        [
            reply("partial", finish="length"),
            reply(calls=[ToolCall("a", "record", {})], finish="length"),
            reply("another part", finish="length"),
        ]
    )
    result = AgentRuntime(model, [tool]).run("task")
    assert result.status == "stopped" and result.steps == 3
    assert not tool.seen
    LLMRequest(result.history)


@pytest.mark.parametrize(
    "failure", [LLMTimeoutError("timeout"), InvalidResponseError("bad response")]
)
def test_recovery_failure_keeps_completed_work_and_does_not_retry(failure):
    tool = RecordingTool()
    model = ScriptedLLM(
        [
            reply(calls=[ToolCall("a", "record", {"value": 1})]),
            reply("part", finish="length"),
            failure,
            reply("continued"),
        ]
    )
    runtime = AgentRuntime(model, [tool])
    result = runtime.run("task")
    assert result.status == "stopped" and result.resumable
    assert "恢复请求失败" in result.notice
    assert len(model.requests) == 3 and len(tool.seen) == 1
    assert any(m.role == "tool" for m in result.history)
    assert runtime.run("继续", history=result.history).status == "completed"
    assert len(tool.seen) == 1


@pytest.mark.parametrize(
    "replies,expected_calls",
    [
        ([reply("", finish="length")], 1),
        ([reply("same", finish="length"), reply("same", finish="length")], 2),
    ],
)
def test_no_text_and_repeated_continuation_stop_early(replies, expected_calls):
    model = ScriptedLLM(replies)
    result = AgentRuntime(model).run("task")
    assert result.status == "stopped" and result.resumable
    assert len(model.requests) == expected_calls
    LLMRequest(result.history)


def test_console_retains_recovery_history_for_next_task(monkeypatch, capsys):
    inputs(monkeypatch, ["task", "继续", "/exit"])
    model = ScriptedLLM([reply("partial", finish="length"), reply("continued")])
    run_interactive(AgentRuntime(model, max_recoveries=0))
    output = capsys.readouterr().out
    assert "上下文已清空" not in output and "尚未完成" in output
    assert any(m.content == "partial" for m in model.requests[-1].messages)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_recoveries": -1},
        {"max_recoveries": True},
        {"max_recoveries": 1.5},
        {"recovery_max_output_tokens": 1},
        {"recovery_max_output_tokens": True},
    ],
)
def test_invalid_recovery_configuration(kwargs):
    with pytest.raises(ValueError):
        AgentRuntime(ScriptedLLM([]), **kwargs)


@pytest.mark.parametrize("finish", ["tool_calls", "stop", "blocked"])
def test_invalid_recovery_response_cannot_poison_saved_history(finish):
    call = ToolCall("same", "record", {})
    tool = RecordingTool()
    model = ScriptedLLM(
        [
            reply(calls=[call]),
            reply("partial", finish="length"),
            reply(calls=[call], finish=finish),
        ]
    )
    result = AgentRuntime(model, [tool]).run("task")
    assert result.resumable and result.status == "stopped"
    assert len(tool.seen) == 1
    LLMRequest(result.history)


def test_recovery_config_roundtrip_and_cli_override(monkeypatch, tmp_path):
    import argparse

    from cli.config import read_config, save_user_config
    from cli.config_command import validate_value, validate_values
    from cli.settings import add_runtime_arguments

    assert validate_value("AGENT_MAX_RECOVERIES", "0") == "0"
    with pytest.raises(ValueError):
        validate_value("AGENT_MAX_RECOVERIES", "-1")
    with pytest.raises(ValueError):
        validate_values(
            {"AGENT_MAX_OUTPUT_TOKENS": "8192", "AGENT_RECOVERY_MAX_OUTPUT_TOKENS": "4096"}
        )
    config = {"AGENT_MAX_RECOVERIES": "1", "AGENT_RECOVERY_MAX_OUTPUT_TOKENS": "16384"}
    saved = save_user_config(config)
    assert read_config(saved) == config
    for key, value in config.items():
        monkeypatch.setenv(key, value)
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--max-recoveries", "0"])
    assert args.max_recoveries == 0 and args.recovery_max_output_tokens == 16384


def test_successful_tool_turn_resets_consecutive_recovery_counter():
    tool = RecordingTool()
    model = ScriptedLLM(
        [
            reply(calls=[ToolCall("a", "record", {})], finish="length"),
            reply(calls=[ToolCall("a", "record", {})]),
            reply("first part", finish="length"),
            reply("second part"),
        ]
    )
    result = AgentRuntime(model, [tool], max_recoveries=1).run("task")
    assert result.status == "completed" and len(tool.seen) == 1
    assert [r["attempt"] for r in result.stats.recoveries] == [1, 1]


def test_recovery_can_be_cancelled_before_next_request():
    runtime = AgentRuntime(ScriptedLLM([reply("partial", finish="length")]))
    cancelled = False

    def on_event(name, stats):
        nonlocal cancelled
        if name == "recovery":
            cancelled = True

    def check():
        if cancelled:
            raise KeyboardInterrupt()

    runtime.on_event = on_event
    runtime.check_cancelled = check
    with pytest.raises(KeyboardInterrupt):
        runtime.run("task")
    assert len(runtime.llm.requests) == 1


def test_full_tui_preserves_partial_text_and_history_after_recovery_limit():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from test_tui import until

    from cli.tui import ConversationUI

    async def run():
        model = ScriptedLLM([reply("partial", finish="length"), reply("continued")])
        runtime = AgentRuntime(model, max_recoveries=0)
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("task\r")
            await until(lambda: len(model.requests) == 1 and not ui.busy)
            assert "尚未完成" in ui.transcript and "上下文已清空" not in ui.transcript
            assert ui.history and ui.phase == "任务尚未完成，可输入“继续”"
            pipe.send_text("继续\r")
            await until(lambda: len(model.requests) == 2 and not ui.busy)
            assert any(m.content == "partial" for m in model.requests[-1].messages)
            assert ui.transcript.count("partial") == ui.transcript.count("continued") == 1
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


@pytest.mark.parametrize("provider,fmt", FORMATS)
def test_http_adapter_runtime_recovery_executes_only_complete_call(provider, fmt):
    from llm import ToolDefinition

    tool = RecordingTool()
    tool.definition = ToolDefinition("read_file", "record request")
    bodies = []
    responses = iter(
        [limited_payload(fmt, malformed=True), payload(fmt), payload(fmt, calls=False)]
    )

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig(provider, "m", api_key="test", stream=False), http_client=http)
        result = AgentRuntime(client, [tool]).run("task")
    assert result.status == "completed" and result.steps == 3
    assert len(bodies) == 3 and len(tool.seen) == 1
    assert tool.seen[0] == {"path": "主程序.py"}
    assert result.stats.token_total("input_tokens") == (60, 3)
    assert "均未执行" in json.dumps(bodies[1], ensure_ascii=False)
    LLMRequest(result.history)
