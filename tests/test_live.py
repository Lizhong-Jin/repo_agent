import argparse
import asyncio
from copy import deepcopy

import httpx
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_streaming import ANTHROPIC, CHAT, GEMINI, RESPONSES, Pieces, chat, sse

from agent import AgentRuntime
from cli.interactive import display_result
from cli.output import LiveOutput
from cli.input import SessionInput
from cli.thinking_control import ThinkingControl
from cli.settings import add_runtime_arguments, request_options
from llm import AsyncLLMClient, ConfigurationError, LLMClient, LLMConfig, LLMRequest, Message
from llm.errors import LLMTimeoutError


def control(provider="zhipu", extra="{}"):
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--extra-json", extra])
    args.provider, args.model = provider, "m"
    runtime = AgentRuntime(object(), request_extra=request_options(args))
    return ThinkingControl(runtime, args)


def test_switch_validates_and_preserves_unrelated_options():
    c = control(extra='{"top_p":0.8}')
    c.command("/thinking on high")
    assert c.runtime.request_extra == {
        "top_p": 0.8,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    snapshot = deepcopy(c.runtime.request_extra)
    with pytest.raises(ConfigurationError):
        c.command("/thinking on high budget=2048")
    assert c.runtime.request_extra == snapshot
    assert c.current["effort"] == "high"
    c.command("/thinking off")
    assert c.runtime.request_extra == {"top_p": 0.8, "thinking": {"type": "disabled"}}
    c.command("/thinking auto")
    assert c.runtime.request_extra == {"top_p": 0.8}


def test_claude_budget_and_native_override():
    c = control("anthropic")
    c.command("/thinking on budget=2048")
    assert c.runtime.request_extra["thinking"]["budget_tokens"] == 2048
    with pytest.raises(ConfigurationError):
        c.command("/thinking on budget=8000")
    c = control(extra='{"thinking":{"type":"enabled"}}')
    with pytest.raises(ConfigurationError):
        c.cycle()


def test_keyboard_switch_retains_draft_and_updates_payload():
    c = control()
    c.model = "glm-5.2"
    with create_pipe_input() as pipe:
        reader = SessionInput(c, terminal_input=pipe, terminal_output=DummyOutput())
        pipe.send_text("draft\x1b[Z remainder\r")
        assert reader.read("你> ") == "draft remainder"
    assert c.current["mode"] == "disabled"
    assert c.runtime.request_extra["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("key,error", [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_input_interrupts(key, error):
    with create_pipe_input() as pipe:
        reader = SessionInput(control(), terminal_input=pipe, terminal_output=DummyOutput())
        pipe.send_text(key)
        with pytest.raises(error):
            reader.read("你> ")


@pytest.mark.parametrize(
    "provider,events,expected",
    [
        ("deepseek", CHAT, "你好"),
        ("anthropic", ANTHROPIC, ""),
        ("openai", RESPONSES, "你好"),
        ("gemini", GEMINI, "你好"),
    ],
)
@pytest.mark.parametrize("async_mode", [False, True])
def test_public_deltas_and_timings(provider, events, expected, async_mode):
    received = []
    source = Pieces(sse(events))

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    def callback(kind, text, elapsed):
        received.append((kind, text, elapsed))

    config = LLMConfig(provider, "m", api_key="key")
    transport = httpx.MockTransport(handler)
    if async_mode:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                return await AsyncLLMClient(config, http_client=http).generate_with_events(
                    LLMRequest([Message("user", "hi")]), callback
                )

        asyncio.run(run())
    else:
        with httpx.Client(transport=transport) as http:
            LLMClient(config, http_client=http).generate_with_events(
                LLMRequest([Message("user", "hi")]), callback
            )
    assert "".join(text for kind, text, _ in received if kind == "text") == expected
    assert received[0][0] == "first_data"
    assert received[-1][0] == "end"
    assert [t for _, _, t in received] == sorted(t for _, _, t in received)
    assert "opaque" not in repr(received)
    thoughts = "".join(text for kind, text, _ in received if kind == "thinking_delta")
    assert thoughts == ("思考" if provider in {"deepseek", "anthropic"} else "")


def test_display_before_stream_finishes_and_no_duplicate(capsys):
    class Source(httpx.SyncByteStream):
        def __iter__(self):
            yield sse([chat({"content": "hello"})])
            assert "hello" in capsys.readouterr().out
            yield sse([chat({"content": " world"}, "stop"), "[DONE]"])

    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Source()
        )
    )
    with httpx.Client(transport=transport) as http:
        runtime = AgentRuntime(
            LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http)
        )
        runtime.on_model_event = LiveOutput()
        result = runtime.run("hello")
    display_result(result)
    output = capsys.readouterr().out
    assert output.count(" world") == 1
    assert "首字" in output and "显示延迟" in output
    record = result.stats.model_calls[0]
    assert record.first_data_seconds <= record.first_text_seconds <= record.first_display_seconds
    assert record.response_seconds >= record.first_text_seconds


def test_partial_failure_keeps_metrics_without_returning_response(capsys):
    source = Pieces(sse([chat({"content": "partial"})]), httpx.ReadTimeout("private"))
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
    )
    with httpx.Client(transport=transport) as http:
        runtime = AgentRuntime(
            LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http)
        )
        runtime.on_model_event = LiveOutput()
        with pytest.raises(LLMTimeoutError):
            runtime.run("hi")
    record = runtime.last_stats.model_calls[0]
    assert record.status == "failed" and record.response_seconds is not None
    assert "partial" in capsys.readouterr().out
    assert source.closed


def test_interactive_settings_reach_requests_and_trace(tmp_path, monkeypatch, capsys):
    import json

    from agent.Tracing import Tracer
    from cli.interactive import run_interactive

    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse([chat({"content": "answer"}, "stop"), "[DONE]"]),
        )

    c = control()
    script = iter(["/thinking on high", "first", "/thinking off", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(script))
    with httpx.Client(transport=httpx.MockTransport(handler)) as http, Tracer(tmp_path) as tracer:
        c.runtime.llm = LLMClient(LLMConfig("zhipu", "m", api_key="key"), http_client=http)
        c.runtime.on_event = tracer
        c.runtime.on_model_event = LiveOutput()
        run_interactive(c.runtime, thinking=c)
    assert seen[0]["reasoning_effort"] == "high"
    assert seen[1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in seen[1]
    assert any(m["content"] == "first" for m in seen[1]["messages"])
    records = [json.loads(line) for line in tracer.jsonl_path.read_text().splitlines()]
    calls = [r["model_call"] for r in records if r["event"] == "model_end"]
    assert [r["thinking"]["mode"] for r in calls] == ["enabled", "disabled"]
    assert all(
        r["first_text_seconds"] is not None and r["response_seconds"] is not None for r in calls
    )
    assert capsys.readouterr().out.count("answer") == 2


def test_session_token_totals_missing_usage_and_duplicate_events(tmp_path):
    from agent.Tracing import ModelCallRecord, RunStats
    from cli.session_status import SessionStatus
    from llm import Usage

    status = SessionStatus(tmp_path)
    assert "输入 0 · 输出 0" in status.describe()
    first = RunStats(1)
    first.model_calls.append(ModelCallRecord(1, usage=Usage(1200, 50)))
    status("model_start", first)
    assert status.calls == 0
    status("model_end", first)
    status("model_end", first)
    first.model_calls.append(ModelCallRecord(2, status="failed"))
    status("model_end", first)
    second = RunStats(2)
    second.model_calls.append(ModelCallRecord(1, usage=Usage(300, 20)))
    status("model_end", second)
    assert status.calls == 3
    assert status.totals == {"input_tokens": 1500, "output_tokens": 70}
    assert "输入 1.5k（部分已知）" in status.describe()
    assert "输出 70（部分已知）" in status.describe()
    assert str(tmp_path.resolve()) in status.describe()


def test_session_unknown_is_not_reported_as_zero(tmp_path):
    from agent.Tracing import ModelCallRecord, RunStats
    from cli.session_status import SessionStatus
    from llm import Usage

    status = SessionStatus(tmp_path)
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(None, 0)))
    status("model_end", stats)
    assert "输入 未知（接口未返回）" in status.describe()
    assert "输出 0" in status.describe()


def test_toolbar_shows_live_totals_and_project_path(tmp_path):
    from agent.Tracing import ModelCallRecord, RunStats
    from cli.session_status import SessionStatus
    from llm import Usage

    status = SessionStatus(tmp_path)
    with create_pipe_input() as pipe:
        reader = SessionInput(
            control(), status=status, terminal_input=pipe, terminal_output=DummyOutput()
        )
        assert "输入 0 · 输出 0" in reader.session.bottom_toolbar()
        stats = RunStats(1)
        stats.model_calls.append(ModelCallRecord(1, usage=Usage(10, 5)))
        status("model_end", stats)
        toolbar = reader.session.bottom_toolbar()
        assert "输入 10 · 输出 5" in toolbar
        assert f"工作目录：{tmp_path.resolve()}" in toolbar
        assert "思考设置：" in toolbar


def test_cli_session_clear_keeps_usage_totals(tmp_path, monkeypatch, capsys):
    # This accounting fixture advertises an intentionally tiny synthetic context limit.
    monkeypatch.setenv("AGENT_AUTO_COMPACT", "false")
    import json

    from cli import main as cli

    responses = iter([(10, 2), (20, 3)])

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m", "context_length": 1000}]})
        input_tokens, output_tokens = next(responses)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "cli.runtime_setup.LLMClient",
        lambda config: LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent",
            "--sandbox",
            "local",
            "--provider",
            "deepseek",
            "--model",
            "m",
            "--root",
            str(tmp_path),
        ],
    )
    inputs = iter(["first", "/clear", "second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    try:
        cli.main()
    finally:
        http.close()
    output = capsys.readouterr().out
    assert "输入 10 · 输出 2" in output
    assert "输入 30 · 输出 5" in output
    assert "服务端自动获取" in output and "≈2.3%" in output
    assert str(tmp_path.resolve()) in output
    records = [
        json.loads(line)
        for file in (tmp_path / "logs").glob("*.trace.jsonl")
        for line in file.read_text().splitlines()
    ]
    assert sum(r["event"] == "model_end" for r in records) == 2


def test_context_uses_latest_round_not_session_total_and_resets(tmp_path):
    from agent.Tracing import ModelCallRecord, RunStats
    from cli.session_status import SessionStatus
    from llm import Usage

    status = SessionStatus(tmp_path, context_window=10000)
    assert "占用未知" in status.describe_context()
    stats = RunStats(1)
    for step, usage in enumerate([Usage(1000, 200), Usage(2000, 500)], 1):
        stats.model_calls.append(ModelCallRecord(step, usage=usage))
        status("model_end", stats)
    assert status.context_tokens == 2500
    assert "≈25.0%" in status.describe()
    assert status.totals == {"input_tokens": 3000, "output_tokens": 700}
    status("tool_end", stats)
    assert "待下轮" in status.describe_context()
    status.context_command("/context 20000")
    assert "≈12.5%" in status.describe_context()
    status.reset_context()
    assert status.context_tokens is None
    assert status.totals["input_tokens"] == 3000
    assert "占用未知" in status.describe_context()


def test_context_missing_usage_clears_old_percentage(tmp_path):
    from agent.Tracing import ModelCallRecord, RunStats
    from cli.session_status import SessionStatus
    from llm import Usage

    status = SessionStatus(tmp_path, context_window=1000)
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(500, 100)))
    status("model_end", stats)
    stats.model_calls.append(ModelCallRecord(2, usage=Usage(600, None)))
    status("model_end", stats)
    assert status.context_tokens is None
    assert "%" not in status.describe_context()
    assert "接口未返回完整用量" in status.describe_context()


@pytest.mark.parametrize("value", [0, -1, True, 1.2, "1000"])
def test_context_window_validation(tmp_path, value):
    from cli.session_status import SessionStatus

    with pytest.raises(ConfigurationError):
        SessionStatus(tmp_path, context_window=value)


def test_context_unknown_limit_and_over_limit_are_explicit(tmp_path):
    from cli.session_status import SessionStatus

    status = SessionStatus(tmp_path)
    status.context_tokens = 1500
    assert "上限未知" in status.describe_context()
    assert "%" not in status.describe_context()
    status.context_command("/context 1000")
    assert "150.0%" in status.describe_context()
    with pytest.raises(ConfigurationError):
        status.context_command("/context -1")
    assert status.context_window == 1000


@pytest.mark.parametrize("model", ["glm-5.3", "glm-5.3-flash"])
def test_glm_53_shortcuts_use_supported_unique_levels(model):
    c = control()
    c.model = model
    assert "max（服务端默认）" in c.describe()
    assert c.presets() == [("auto", None, None)] + [
        ("enabled", level, None) for level in ("low", "high", "max")
    ]
    for level in ("low", "high", "max"):
        c.cycle()
        assert c.runtime.request_extra["reasoning_effort"] == level
    c.cycle()
    assert c.runtime.request_extra == {}
    c.command("/thinking on medium")
    assert c.current["effort"] == "high"
    assert c.runtime.thinking_settings["effort"] == "high"
    assert c.runtime.request_extra["reasoning_effort"] == "high"
    before = deepcopy((c.current, c.runtime.request_extra, c.runtime.thinking_settings))
    with pytest.raises(ConfigurationError, match="不能关闭思考"):
        c.command("/thinking off")
    assert (c.current, c.runtime.request_extra, c.runtime.thinking_settings) == before


def test_glm_53_initial_alias_display_matches_payload():
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--thinking", "enabled", "--reasoning-effort", "medium"])
    args.provider, args.model = "zhipu", "glm-5.3"
    runtime = AgentRuntime(object(), request_extra=request_options(args))
    c = ThinkingControl(runtime, args)
    assert "强度=high" in c.describe()
    assert (
        runtime.thinking_settings["effort"] == runtime.request_extra["reasoning_effort"] == "high"
    )
