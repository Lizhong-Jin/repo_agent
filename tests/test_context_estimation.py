"""Startup estimates include loaded context but never count as billed usage."""

from dataclasses import replace

from agent import AgentRuntime
from agent.Tracing import ModelCallRecord, RunStats
from cli.live import SessionStatus
from llm import Message, ToolCall, Usage
from llm.schemas import ProviderState, ToolDefinition
from llm.token_estimation import estimate_context_tokens


class OfflineModel:
    def generate(self, request):
        raise AssertionError("Startup must not call the model")


class Tool:
    definition = ToolDefinition("read_file", "Read a file", {
        "type": "object", "properties": {"path": {"type": "string"}},
    })


def test_startup_counts_system_tools_and_skills_without_requests(tmp_path):
    class Skills:
        def prompt(self):
            return "技能目录：" + "分析算子性能" * 200

    runtime = AgentRuntime(OfflineModel(), tools=[Tool()], system_prompt="中文提示" * 100)
    baseline = runtime.estimate_context_tokens()
    assert baseline > AgentRuntime(OfflineModel(), system_prompt="short").estimate_context_tokens()
    no_tools = AgentRuntime(OfflineModel(), system_prompt=runtime.system_prompt)
    assert baseline > no_tools.estimate_context_tokens()
    # Use the request-only skill prompt path without touching the filesystem.
    runtime.skills = Skills()
    status = SessionStatus(tmp_path, context_window=10000)
    status.initialize_context(runtime)
    assert status.context_tokens > baseline
    assert "本地粗估" in status.describe_context(compact=True)
    assert "%" in status.describe_context(compact=True)
    assert "未知" not in status.describe_context(compact=True)
    assert status.calls == 0 and not any(status.totals.values())
    assert not any(status.reported.values())


def test_history_counts_tool_arguments_results_without_duplicating_native_state():
    call = ToolCall("one", "read_file", {"path": "example.txt"})
    assistant = Message("assistant", "查看文件", tool_calls=(call,))
    history = (
        Message("system", "system"), Message("user", "read"), assistant,
        Message.tool_result(call, "文件内容" * 500),
    )
    runtime = AgentRuntime(OfflineModel(), tools=[Tool()])
    before = [message.to_dict() for message in history]
    assert runtime.estimate_context_tokens(history) > runtime.estimate_context_tokens(history[:-1])
    native = replace(assistant, provider_state=ProviderState(
        "openai", "m", {"encrypted_content": "opaque" * 1000}, assistant.fingerprint(),
    ))
    assert estimate_context_tokens(history) == estimate_context_tokens(
        (*history[:2], native, history[3])
    )
    assert [message.to_dict() for message in history] == before


def test_server_usage_replaces_estimate_and_missing_usage_does_not_reuse_it(tmp_path):
    runtime = AgentRuntime(OfflineModel())
    status = SessionStatus(tmp_path, context_window=10000)
    status.initialize_context(runtime)
    stats = RunStats(1)
    status("model_start", stats)
    assert "本地粗估" in status.describe_context(compact=True)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(123, 45)))
    status("model_end", stats)
    assert status.context_tokens == 168
    assert "本地粗估" not in status.describe_context(compact=True)
    status.initialize_context(runtime)
    assert status.context_tokens == 168
    stats.model_calls.append(ModelCallRecord(2, usage=Usage(None, None)))
    status("model_end", stats)
    assert status.context_tokens is None
    assert "占用未知" in status.describe_context()


def test_unknown_window_does_not_prevent_estimation_or_invent_percentage(tmp_path):
    status = SessionStatus(tmp_path)
    status.initialize_context(AgentRuntime(OfflineModel()))
    assert status.context_tokens > 0
    assert "上限未知" in status.describe_context(compact=True)
    assert "%" not in status.describe_context(compact=True)


def test_input_only_server_usage_survives_startup(tmp_path):
    status = SessionStatus(tmp_path, context_window=10000)
    status.context_limit_kind = "input"
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(123, None)))
    status("model_end", stats)
    status.initialize_context(AgentRuntime(OfflineModel()))
    assert status.context_input_tokens == 123
    assert "本地粗估" not in status.describe_context()
