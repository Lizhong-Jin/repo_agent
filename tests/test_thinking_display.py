import argparse
import asyncio
import threading
from copy import deepcopy

import httpx
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_streaming import ANTHROPIC, CHAT, GEMINI, RESPONSES, Pieces, chat, sse
from test_tui import until

from agent import AgentRuntime
from agent.Tracing import Tracer
from agent.transcript import Transcript
from cli.config_command import validate_value
from cli.output import LiveOutput
from cli.settings import add_runtime_arguments
from cli.thinking_display import ThinkingDisplay
from cli.terminal.application import ConversationUI
from configuration.environment import read_config, user_config_path
from llm import (
    AsyncLLMClient,
    LLMClient,
    LLMConfig,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    Message,
    Usage,
)

REQUEST = LLMRequest([Message("user", "hello")])


def thought_events(provider):
    events = deepcopy(
        {"deepseek": CHAT, "anthropic": ANTHROPIC, "openai": RESPONSES, "gemini": GEMINI}[provider]
    )
    if provider == "openai":
        events[:0] = [
            {"type": "response.reasoning_summary_text.delta", "delta": "思"},
            {"type": "response.reasoning_summary_text.delta", "delta": "考"},
            {"type": "response.reasoning_summary_text.done", "text": "思考"},
        ]
    elif provider == "gemini":
        events[:0] = [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "思考", "thoughtSignature": "opaque"},
                            ]
                        }
                    }
                ]
            }
        ]
    return events


@pytest.mark.parametrize("provider", ["deepseek", "anthropic", "openai", "gemini"])
@pytest.mark.parametrize("async_mode", [False, True])
def test_visible_thinking_separate_ordered_and_native_state_preserved(provider, async_mode):
    events = thought_events(provider)
    received = []

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Pieces(sse(events))
        )

    def callback(*event):
        received.append(event)

    config = LLMConfig(provider, "m", api_key="key")
    transport = httpx.MockTransport(handler)
    if async_mode:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                return await AsyncLLMClient(config, http_client=http).generate_with_events(
                    REQUEST, callback
                )

        response = asyncio.run(run())
    else:
        with httpx.Client(transport=transport) as http:
            response = LLMClient(config, http_client=http).generate_with_events(REQUEST, callback)
    assert "".join(t for k, t, _ in received if k == "thinking_delta") == "思考"
    assert "思考" not in response.text
    kinds = [k for k, _, _ in received]
    assert kinds.count("first_thinking") == 1
    assert kinds.index("first_thinking") < kinds.index("thinking_delta")
    assert kinds.count("thinking_start") == kinds.count("thinking_end") == 1
    if "first_text" in kinds:
        assert kinds.index("thinking_end") < kinds.index("first_text")
    assert kinds[-1] == "end"
    assert "opaque" not in repr(received)
    # A provider's signed/encrypted native state remains in the model history.
    state = response.message.provider_state.payload
    assert "opaque" in str(state) if provider != "deepseek" else "思考" in str(state)


@pytest.mark.parametrize(
    "provider,data",
    [
        (
            "deepseek",
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "answer",
                            "reasoning_content": "visible",
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
        (
            "anthropic",
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "visible", "signature": "opaque"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "end_turn",
            },
        ),
        (
            "openai",
            {
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "visible"}],
                        "encrypted_content": "opaque",
                    },
                    {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
                ],
            },
        ),
        (
            "gemini",
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"thought": True, "text": "visible", "thoughtSignature": "opaque"},
                                {"text": "answer"},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        ),
    ],
)
@pytest.mark.parametrize("async_mode", [False, True])
def test_nonstream_thinking_is_shown_once_before_answer(provider, data, async_mode):
    received = []
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    config = LLMConfig(provider, "m", api_key="key", stream=False)
    if async_mode:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                return await AsyncLLMClient(config, http_client=http).generate_with_events(
                    REQUEST, lambda *e: received.append(e)
                )

        result = asyncio.run(run())
    else:
        with httpx.Client(transport=transport) as http:
            result = LLMClient(config, http_client=http).generate_with_events(
                REQUEST, lambda *e: received.append(e)
            )
    assert [(k, t) for k, t, _ in received if t] == [
        ("thinking_delta", "visible"),
        ("text", "answer"),
    ]
    assert result.text == "answer"
    assert "opaque" not in repr(received)


@pytest.mark.parametrize(
    "provider,model,extra,path,value",
    [
        ("openai", "gpt-5.2", {}, ("reasoning", "summary"), "auto"),
        (
            "openai",
            "gpt-5.2",
            {"reasoning": {"effort": "high", "summary": "concise"}},
            ("reasoning", "summary"),
            "concise",
        ),
        (
            "gemini",
            "gemini-2.5-flash",
            {},
            ("generationConfig", "thinkingConfig", "includeThoughts"),
            True,
        ),
        (
            "gemini",
            "gemini-2.5-flash",
            {
                "generationConfig": {
                    "thinkingConfig": {"includeThoughts": False, "thinkingBudget": 2048}
                }
            },
            ("generationConfig", "thinkingConfig", "includeThoughts"),
            False,
        ),
        (
            "anthropic",
            "claude-opus-4-6",
            {"thinking": {"type": "adaptive"}},
            ("thinking", "display"),
            "summarized",
        ),
        (
            "anthropic",
            "claude-opus-4-6",
            {"thinking": {"type": "adaptive", "display": "omitted"}},
            ("thinking", "display"),
            "omitted",
        ),
    ],
)
def test_summary_opt_in_preserves_effort_budget_and_explicit_overrides(
    provider, model, extra, path, value
):
    before = deepcopy(extra)
    with httpx.Client() as http:
        client = LLMClient(
            LLMConfig(provider, model, api_key="key", include_thinking=True), http_client=http
        )
        body = client._body(LLMRequest(REQUEST.messages, extra=extra))
    found = body
    for key in path:
        found = found[key]
    assert found == value
    assert extra == before
    for container in ("reasoning", "thinking", "generationConfig"):
        if container in extra:
            for key, val in extra[container].items():
                if key != "thinkingConfig":
                    assert body[container][key] == val


@pytest.mark.parametrize(
    "provider,model,extra",
    [
        ("openai", "private-model", {}),
        ("openai", "gpt-4o", {}),
        ("openai", "gpt-5.2", {"reasoning": {"effort": "none"}}),
        (
            "gemini",
            "gemini-2.5-flash",
            {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
        ),
        ("anthropic", "claude-opus-4-6", {"thinking": {"type": "disabled"}}),
    ],
)
def test_no_speculative_summary_for_unknown_or_disabled_models(provider, model, extra):
    with httpx.Client() as http:
        config = LLMConfig(provider, model, api_key="key", include_thinking=True)
        body = LLMClient(config, http_client=http)._body(LLMRequest(REQUEST.messages, extra=extra))
    assert "summary" not in body.get("reasoning", {})
    assert "display" not in body.get("thinking", {})
    assert "includeThoughts" not in body.get("generationConfig", {}).get("thinkingConfig", {})


def test_thinking_only_failure_ends_block_and_records_partial_timing_without_retry(tmp_path):
    requests, received = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Pieces(
                sse([chat({"reasoning_content": "partial-thought"})]), httpx.ReadTimeout("private")
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http, Tracer(tmp_path) as trace:
        runtime = AgentRuntime(
            LLMClient(LLMConfig("zhipu", "glm-5.3", api_key="key"), http_client=http),
            on_event=trace,
        )
        runtime.on_model_event = lambda *event: received.append(event)
        with pytest.raises(LLMTimeoutError):
            runtime.run("task")
        record = runtime.last_stats.model_calls[0]
        assert record.first_thinking_seconds is not None and record.first_text_seconds is None
        assert record.thinking_characters == len("partial-thought")
        assert record.first_display_seconds is None
        assert [e[0] for e in received][-2:] == ["thinking_end", "end"]
    assert len(requests) == 1
    content = trace.jsonl_path.read_text()
    assert '"first_thinking_seconds"' in content and "partial-thought" not in content


def test_display_persists_independently_and_failed_save_rolls_back(monkeypatch):
    display = ThinkingDisplay()
    display.set("expanded")
    assert read_config(user_config_path())["AGENT_THINKING_DISPLAY"] == "expanded"
    monkeypatch.setenv("AGENT_THINKING_DISPLAY", "hidden")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    assert parser.parse_args([]).thinking_display == "hidden"
    assert parser.parse_args(["--thinking-display", "expanded"]).thinking_display == "expanded"
    assert not getattr(
        parser.parse_args(["--thinking-display", "hidden"]), "thinking_explicit", False
    )
    with pytest.raises(ValueError):
        validate_value("AGENT_THINKING_DISPLAY", "invalid")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr("cli.thinking_display.save_user_config", fail)
    with pytest.raises(OSError):
        display.toggle()
    assert display.mode == "expanded"


def test_long_collapsed_thought_not_materialized_and_highlighting_survives_toggle():
    transcript = Transcript()
    transcript.append("你> 用户\n", kind="user")
    transcript.thinking("thinking_start", "", 1, 1.0)
    for _ in range(10000):
        transcript.thinking("thinking_delta", "思考片段", 1, 2.0)
    transcript.thinking("thinking_end", "", 1, 10.0)
    transcript.append("正文\n")
    transcript.append("你> 下一轮\n", kind="user")
    thought = transcript.blocks[1]
    text, users, _ = transcript.render("collapsed")
    assert "40,000 字符" in text and "思考片段" not in text
    assert not thought._text and len(thought.chunks) == 10000
    for mode in ("expanded", "hidden", "collapsed", "expanded"):
        text, users, thoughts = transcript.render(mode)
        assert text.count("思考片段") == (10000 if mode == "expanded" else 0)
        assert [text.splitlines()[i] for i in users] == ["你> 用户", "你> 下一轮"]
        assert text.count("正文") == 1
        assert bool(thoughts) == (mode != "hidden")


class SlowThinkingModel:
    def __init__(self):
        self.release = threading.Event()
        self.requests = []

    def generate_with_events(self, request, callback):
        self.requests.append(request)
        try:
            callback("first_data", "", 0.01)
            callback("first_thinking", "", 0.02)
            callback("thinking_start", "", 0.02)
            callback("thinking_delta", "可显示的思考片段", 0.03)
            for _ in range(400):
                if self.release.wait(0.005):
                    break
                callback("thinking_delta", "。", 0.04)
            callback("thinking_end", "", 0.1)
            callback("first_text", "", 0.11)
            callback("text", "正式回复", 0.11)
            return LLMResponse(
                "zhipu",
                "glm-5.3",
                Message("assistant", "正式回复"),
                "stop",
                Usage(10, 100, reasoning_tokens=90),
            )
        finally:
            callback("thinking_end", "", 0.15)
            callback("end", "", 0.2)


def test_live_toggle_keeps_draft_and_request_and_records_correct_timing():
    async def run():
        model = SlowThinkingModel()
        runtime = AgentRuntime(model)
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
            task = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("任务\r")
                await until(lambda: "已接收" in ui.transcript)
                assert ui.busy and "可显示的思考片段" not in ui.transcript
                assert "思考已接收" in ui.phase_text()
                pipe.send_text("草稿\x14")
                await until(lambda: "可显示的思考片段" in ui.transcript)
                assert ui.editor.text == "草稿"
                pipe.send_text("\x14")
                await until(lambda: "可显示的思考片段" not in ui.transcript)
                model.release.set()
                await until(lambda: not ui.busy)
                assert len(model.requests) == 1
                assert ui.transcript.count("正式回复") == 1
                assert "其中思考 90" in ui.transcript
                record = runtime.last_stats.model_calls[0]
                assert record.first_thinking_seconds == 0.02
                assert record.first_text_seconds == 0.11
                assert record.first_display_seconds is not None
                pipe.send_text("\x03/thinking display hidden\r")
                await until(lambda: ui.display.mode == "hidden")
                assert "思考 · 模型" not in ui.transcript
                pipe.send_text("\x14")
                await until(lambda: "可显示的思考片段" in ui.transcript)
                assert read_config(user_config_path())["AGENT_THINKING_DISPLAY"] == "expanded"
                assert "可显示的思考片段" not in str(ui.history)
            finally:
                model.release.set()
                await until(lambda: not ui.busy)
                pipe.send_text("\x03/exit\r")
                await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_cancel_during_thinking_preserves_partial_view_and_no_answer():
    async def run():
        model = SlowThinkingModel()
        runtime = AgentRuntime(model)
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                runtime,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
                display=ThinkingDisplay("expanded"),
            )
            task = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("任务\r")
                await until(lambda: "可显示的思考片段" in ui.transcript)
                pipe.send_text("草稿\x03")
                await until(lambda: not ui.busy)
                assert "可显示的思考片段" in ui.transcript
                assert "正式回复" not in ui.transcript
                assert "已中断" in ui.transcript
                assert ui.editor.text == "草稿" and not ui.history
                assert runtime.last_stats.model_calls[0].first_text_seconds is None
            finally:
                model.release.set()
                pipe.send_text("\x03/exit\r")
                await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_signature_only_reports_unavailable_without_revealing_payload(capsys):
    response = {
        "status": "completed",
        "output": [
            {"type": "reasoning", "encrypted_content": "DO-NOT-DISPLAY"},
            {"type": "message", "content": [{"type": "output_text", "text": "answer"}]},
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 90},
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
    ) as http:
        client = LLMClient(LLMConfig("openai", "gpt-5.2", api_key="key"), http_client=http)
        runtime = AgentRuntime(client)
        runtime.on_model_event = LiveOutput(display=ThinkingDisplay("expanded"))
        runtime.run("task")
    out = capsys.readouterr().out
    assert "接口未提供可显示的思考内容" in out and "DO-NOT-DISPLAY" not in out
    assert out.count("answer") == 1


def test_interleaved_gemini_parts_keep_display_order():
    from llm.events import RequestEvents
    from llm.streaming import StreamAssembler

    received = []
    events = RequestEvents(lambda kind, text, elapsed: received.append((kind, text)))
    assembler = StreamAssembler("gemini", events.text, events.thinking, events.end_thinking)
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"thought": True, "text": "first thought"},
                        {"text": "first answer"},
                        {"thought": True, "text": "second thought"},
                        {"text": "second answer"},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }
    for line in sse([data]).decode().splitlines():
        assembler.feed(line)
    assembler.finish()
    assert [(kind, text) for kind, text in received if text] == [
        ("thinking_delta", "first thought"),
        ("text", "first answer"),
        ("thinking_delta", "second thought"),
        ("text", "second answer"),
    ]
    assert [kind for kind, _ in received].count("first_thinking") == 1
    assert [kind for kind, _ in received].count("thinking_start") == 2


def test_redacted_anthropic_block_without_usage_is_not_displayed(capsys):
    data = {
        "role": "assistant",
        "content": [
            {"type": "redacted_thinking", "data": "HIDDEN-SIGNATURE"},
            {"type": "text", "text": "answer"},
        ],
        "stop_reason": "end_turn",
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    ) as http:
        runtime = AgentRuntime(
            LLMClient(LLMConfig("anthropic", "m", api_key="key"), http_client=http)
        )
        runtime.on_model_event = LiveOutput(display=ThinkingDisplay("expanded"))
        runtime.run("task")
    output = capsys.readouterr().out
    assert "接口未提供可显示的思考内容" in output
    assert "HIDDEN-SIGNATURE" not in output
    assert runtime.last_stats.model_calls[0].thinking_available is False


def test_terminal_control_characters_are_display_only():
    from agent.transcript import display_text

    raw = "a\x1b[2J\x1b]52;c;secret\x07b\rnext\nline"
    displayed = display_text(raw)
    assert "\x1b" not in displayed and "\x07" not in displayed and "\r" not in displayed
    assert "\nline" in displayed
    assert raw.startswith("a\x1b")


@pytest.mark.parametrize(
    "mode,visible", [("expanded", True), ("collapsed", False), ("hidden", False)]
)
def test_plain_output_display_modes_do_not_duplicate_answer(mode, visible, capsys):
    data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "visible thought",
                },
                "finish_reason": "stop",
            }
        ]
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))
    ) as http:
        runtime = AgentRuntime(
            LLMClient(LLMConfig("zhipu", "glm-5.3", api_key="key"), http_client=http)
        )
        runtime.on_model_event = LiveOutput(display=ThinkingDisplay(mode))
        result = runtime.run("task")
    output = capsys.readouterr().out
    assert ("visible thought" in output) is visible
    assert output.count("answer") == 1
    assert not result.undisplayed_text
    assert result.history[-1].provider_state.payload["reasoning_content"] == "visible thought"
