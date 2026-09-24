"""Lossy context reduction must never destroy originals, replay tools or lose usage."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent import AgentRuntime
from agent.compaction import SECTIONS, CompactionSettings
from agent.history import HistoryArchive, HistoryTool
from agent.session import SessionStore
from cli.live import SessionStatus
from cli.session import SavedConversation
from cli.tui import ConversationUI
from llm import LLMConfig, LLMRequest, LLMResponse, Message, ToolCall, Usage
from llm.schemas import ProviderState
from tools import ToolResult


class Model:
    def __init__(self):
        self.config = LLMConfig("deepseek", "test", api_key="fake")
        self.requests = []
        self.invalid = False
        self.normal = []
        self.summary_hook = None

    def generate(self, request):
        self.requests.append(request)
        if request.tool_choice == "none":
            assert not request.tools
            if self.summary_hook:
                self.summary_hook(request)
            records = json.loads(request.messages[-1].content)["records"]
            summary = {key: [] for key in SECTIONS}
            summary["progress"] = [
                {"text": "已读取文件；需继续验证。", "refs": [records[0]["ref"]]}
            ]
            content = "bad json" if self.invalid else json.dumps(summary, ensure_ascii=False)
            message = Message("assistant", content)
        else:
            message = self.normal.pop(0) if self.normal else Message("assistant", "已完成")
        return LLMResponse(
            "deepseek",
            "test",
            message,
            "tool_calls" if message.tool_calls else "stop",
            usage=Usage(100, 20),
        )


@pytest.fixture
def conversation(tmp_path):
    stores = []

    def create(*, new=False, settings=None, project=None, window=24000):
        project = project or tmp_path / "project"
        project.mkdir(exist_ok=True)
        store = SessionStore(project, new=new, directory=tmp_path / "state").open()
        stores.append(store)
        status = SessionStatus(project, context_window=window)
        model = Model()
        archive = HistoryArchive(store)
        runtime = AgentRuntime(
            model,
            tools=[HistoryTool(archive, "search"), HistoryTool(archive, "read")],
            system_prompt="你是编码助手",
            max_output_tokens=1000,
            on_event=status,
        )
        conv = SavedConversation(
            store,
            runtime,
            model.config,
            status,
            compaction_settings=settings
            or CompactionSettings(auto=False, keep_tokens=512, summary_tokens=1500),
        )
        conv.checkpoint(strict=True)
        return conv

    yield create
    for store in stores:
        store.close()


def large_history():
    call = ToolCall("old-read", "read_file", {"path": "old.py"})
    recent = ToolCall("recent-read", "read_file", {"path": "new.py"})
    assistant = Message("assistant", tool_calls=(recent,))
    assistant = replace(
        assistant,
        provider_state=ProviderState(
            "deepseek", "test", {"opaque": "unchanged"}, assistant.fingerprint()
        ),
    )
    return (
        Message("system", "你是编码助手"),
        Message("user", "要求兼容 Python 3.11，不要删除用户修改。"),
        Message("assistant", tool_calls=(call,)),
        Message.tool_result(call, "ORIGINAL_MARKER\n" + "long original file line\n" * 1800),
        Message("assistant", "已读取"),
        Message("user", "接下来修复登录"),
        assistant,
        Message.tool_result(recent, "recent original"),
    )


def test_manual_archives_original_pins_native_tail_and_restores(conversation):
    conv = conversation()
    original = large_history()
    conv.checkpoint(history=original, strict=True)
    conv.status.totals.update(input_tokens=500, output_tokens=50)
    conv.status.calls = 2
    conv.status.reported.update(input_tokens=2, output_tokens=2)
    result = conv.compact()
    assert "→" in result
    assert conv.history[-2:] == original[-2:]
    assert any("不要删除用户修改" in m.content for m in conv.history)
    assert "ORIGINAL_MARKER" not in "".join(m.content for m in conv.history)
    assert conv.status.totals == {"input_tokens": 600, "output_tokens": 70}
    assert conv.status.calls == 3
    assert conv.status.context_tokens == conv.runtime.estimate_context_tokens(conv.history)
    assert conv.runtime._task_number == 0
    LLMRequest(conv.history)
    found = conv.archive.search("ORIGINAL_MARKER")
    ref = found["matches"][0]["reference"]
    read = conv.archive.read(ref)
    assert read["text"].startswith("ORIGINAL_MARKER") and read["next_offset"] is not None
    with conv.archive.connect() as db:
        raw = json.loads(conv.archive._row(db, ref)["raw"])
        assert Message.from_dict(raw) == original[3]
        snapshot = db.execute(
            "SELECT message_ids FROM snapshots WHERE id=?", (conv.compaction_state["snapshot"],)
        ).fetchone()
        assert len(json.loads(snapshot[0])) == len(original)
    saved = conv.history
    conv.store.close()
    restored = conversation()
    assert restored.history == saved
    assert restored.compaction_state["snapshot"] == conv.compaction_state["snapshot"]
    assert restored.status.calls == 3
    assert restored.archive.read(ref)["text"] == read["text"]


def test_bad_summary_keeps_original_but_counts_usage_and_archives(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    old = conv.history
    conv.runtime.llm.invalid = True
    with pytest.raises(ValueError, match="摘要结构|summary structure"):
        conv.compact()
    assert conv.history == old and conv.compaction_state is None
    assert conv.status.totals == {"input_tokens": 100, "output_tokens": 20}
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]
    assert conv.store.data["status"]["calls"] == 1


def test_save_failure_keeps_working_history_and_archive(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    old = conv.history
    monkeypatch.setattr(
        conv.store, "save", lambda data: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError):
        conv.compact()
    assert conv.history == old and conv.compaction_state is None
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]


def test_automatic_compacts_during_tool_loop_and_preserves_pending_task(conversation):
    conv = conversation(
        settings=CompactionSettings(auto=True, keep_tokens=512, summary_tokens=1500)
    )

    class LargeTool:
        definition = HistoryTool(conv.archive, "read").definition
        executions = 0

        def execute(self, arguments):
            self.executions += 1
            return ToolResult(True, {"text": "new large tool output\n" * 4000})

    tool = LargeTool()
    conv.runtime._tools[tool.definition.name] = tool
    call = ToolCall("new-call", "history_read", {"reference": "unused"})
    conv.runtime.llm.normal = [
        Message("assistant", tool_calls=(call,)),
        Message("assistant", "完成"),
    ]
    conv.start_task("读取并修复，不删除文件")
    result = conv.runtime.run("读取并修复，不删除文件", history=conv.history)
    conv.finish_task(result)
    assert tool.executions == 1
    assert conv.compaction_state and conv.pending_task is None
    assert conv.status.calls >= 3
    assert conv.runtime.last_stats.model_calls[-1].purpose == "task"
    assert conv.archive.search("new large tool output")["matches"]
    LLMRequest(conv.history)


def test_auto_off_or_unknown_limit_does_not_generate_summary(conversation):
    conv = conversation()
    original = large_history()
    assert conv.compactor.before_request(original, 1000) is original
    conv.compactor.settings = CompactionSettings(auto=True)
    conv.status.context_window = None
    assert conv.compactor.before_request(original, 1000) is original
    assert conv.runtime.llm.requests == []
    with pytest.raises(ValueError, match="上限未知|upper limit of context is unknown"):
        conv.compactor.compact(original)


def test_repeated_compaction_keeps_stable_references_and_old_originals(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    conv.compact()
    ref = conv.archive.search("ORIGINAL_MARKER")["matches"][0]["reference"]
    conv.history += (
        Message("assistant", "another large result " * 2000),
        Message("user", "继续"),
        Message("assistant", "近期回复"),
    )
    conv.compact()
    assert conv.archive.read(ref)["text"].startswith("ORIGINAL_MARKER")
    assert conv.archive.search("ORIGINAL_MARKER")["matches"][0]["reference"] == ref
    assert sum(pin["text"].startswith("要求兼容") for pin in conv.compaction_state["pins"]) == 1


def test_search_scope_pagination_path_protection_and_no_native_payload(conversation, tmp_path):
    conv = conversation()
    conv.archive.archive(large_history())
    ref = conv.archive.search("recent original")["matches"][0]["reference"]
    _, ids = conv.archive.archive(large_history())
    native_ref = conv.archive.ref(ids[-2])
    assert "opaque" not in json.dumps(conv.archive.read(native_ref))
    old_id = conv.store.id
    conv.new_session()
    assert not conv.archive.search("recent original")["matches"]
    assert conv.archive.search("recent original", session="project")["matches"]
    assert conv.archive.search("recent original", session=old_id)["matches"]
    tool = HistoryTool(conv.archive, "read")
    assert not tool.execute({"reference": "../../.env"}).success
    assert not tool.execute({"reference": ref, "limit": 9000}).success
    assert not tool.execute({"reference": ref, "offset": -1}).success
    page = conv.archive.search("original", session="project", limit=1)
    assert page["next_offset"] == 1
    assert conv.archive.search("original", session="project", limit=1, offset=1)["matches"]
    other = conversation(project=tmp_path / "other")
    assert not HistoryTool(other.archive, "read").execute({"reference": ref}).success
    assert os.stat(conv.archive.path).st_mode & 0o777 == 0o600


def test_archive_rejects_symlinks_and_hardlinks(conversation, tmp_path):
    conv = conversation()
    target = tmp_path / "target"
    target.write_text("untouched")
    conv.archive.path.symlink_to(target)
    with pytest.raises(OSError):
        conv.archive.archive(large_history())
    assert target.read_text() == "untouched"
    conv.archive.path.unlink()
    os.link(target, conv.archive.path)
    with pytest.raises(ValueError):
        conv.archive.archive(large_history())


def test_large_user_constraints_are_never_silently_dropped(conversation):
    conv = conversation()
    old = large_history()
    old = (old[0], Message("user", "关键要求" * 10000), *old[2:])
    conv.checkpoint(history=old, strict=True)
    with pytest.raises(ValueError, match="用户原文|user.s input"):
        conv.compact()
    assert conv.history == old and not conv.runtime.llm.requests
    assert conv.archive.search("关键要求")["matches"]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(target=0.8),
        dict(threshold=float("nan")),
        dict(keep_tokens=1),
        dict(summary_tokens=0),
        dict(auto="yes"),
    ],
)
def test_settings_validation(kwargs):
    with pytest.raises(ValueError):
        CompactionSettings(**kwargs)


def test_tui_manual_command_does_not_submit_task_or_show_summary_json(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)

    async def run():
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                conv.runtime,
                status=conv.status,
                conversation=conv,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(ui.run_async())

            async def until(predicate):
                for _ in range(400):
                    if predicate():
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("UI timeout: " + ui.transcript)

            await until(lambda: ui.app.is_running)
            pipe.send_text("/compact\r")
            await until(lambda: conv.compaction_state is not None and not ui.busy)
            assert "上下文已压缩" in ui.transcript
            assert '"progress"' not in ui.transcript
            assert conv.runtime._task_number == 0 and conv.pending_task is None
            assert ui.history == conv.history
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_archive_is_committed_before_model_runs(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)

    def check(request):
        refs = conv.archive.search("ORIGINAL_MARKER")["matches"]
        assert refs and conv.archive.read(refs[0]["reference"])["text"]
        assert conv.compaction_state is None

    conv.runtime.llm.summary_hook = check
    conv.compact()


def test_cancellation_preserves_history_and_usage(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    old = conv.history
    cancelled = False

    def model_hook(request):
        nonlocal cancelled
        cancelled = True

    def check():
        if cancelled:
            raise KeyboardInterrupt

    conv.runtime.llm.summary_hook = model_hook
    conv.runtime.check_cancelled = check
    with pytest.raises(KeyboardInterrupt):
        conv.compact()
    assert conv.history == old
    assert conv.store.data["status"]["totals"]["input_tokens"] == 100


def test_archive_failure_never_calls_model(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    old = conv.history

    def fail(messages):
        raise OSError("disk full")

    monkeypatch.setattr(conv.archive, "archive", fail)
    with pytest.raises(OSError):
        conv.compact()
    assert conv.history == old and not conv.runtime.llm.requests


def test_input_only_limits_do_not_reserve_output_twice(conversation):
    conv = conversation()
    assert conv.compactor.input_budget(2000) == 22000
    conv.status.context_limit_kind = "input"
    assert conv.compactor.input_budget(2000) == 24000


def test_read_tools_are_read_only_and_empty_search_creates_nothing(conversation):
    conv = conversation()
    assert conv.archive.search("test") == {"matches": [], "next_offset": None}
    assert not conv.archive.path.exists()
    conv.archive.archive(large_history())
    with pytest.raises(OSError):
        with conv.archive.connect() as db:
            db.execute("DELETE FROM messages")
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]


def test_restored_prefix_is_recognized_on_second_compaction(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    conv.compact()
    conv.store.close()
    restored = conversation()
    restored.history += (
        Message("user", "补充新的要求"),
        Message("assistant", "large recent result " * 2000),
        Message("user", "继续"),
        Message("assistant", "目前状态"),
    )
    restored.compact()
    pins = restored.compaction_state["pins"]
    assert sum(p["text"].startswith("要求兼容") for p in pins) == 1
    assert all(not p["text"].startswith("[历史交接资料") for p in pins)


def test_short_history_is_noop_and_corrupt_compaction_metadata_rejected(conversation):
    from agent.session import validate_compaction

    conv = conversation()
    conv.history = (Message("user", "hello"),)
    with pytest.raises(ValueError, match="无需压缩|No need for compression"):
        conv.compact()
    assert not conv.runtime.llm.requests
    with pytest.raises(ValueError, match="压缩元数据"):
        validate_compaction({"prefix": "invalid"}, [m.to_dict() for m in conv.history])


def test_config_cli_and_cross_field_validation():
    import argparse

    from cli.config_command import validate_value, validate_values
    from cli.settings import add_runtime_arguments

    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--auto-compact", "false", "--compact-target", "0.5"])
    assert args.auto_compact is False and args.compact_target == 0.5
    assert validate_value("AGENT_AUTO_COMPACT", "true") == "true"
    for key, value in [
        ("AGENT_COMPACT_THRESHOLD", "nan"),
        ("AGENT_COMPACT_TARGET", "1"),
        ("AGENT_COMPACT_KEEP_TOKENS", "10"),
    ]:
        with pytest.raises(ValueError):
            validate_value(key, value)
    with pytest.raises(ValueError, match="target"):
        validate_values({"AGENT_COMPACT_THRESHOLD": "0.4", "AGENT_COMPACT_TARGET": "0.6"})


def test_non_tui_manual_command(conversation, monkeypatch, capsys):
    from cli.interactive import run_interactive

    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    inputs = iter(["/compact", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    run_interactive(conv.runtime, conversation=conv, status=conv.status)
    assert "上下文已压缩" in capsys.readouterr().out
    assert conv.compaction_state and conv.runtime._task_number == 0


def test_resume_rejects_missing_original_archive(conversation):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    conv.compact()
    conv.store.close()
    conv.archive.path.unlink()
    with pytest.raises(ValueError, match="原文档案缺失|original archive was missing"):
        conversation()


def test_model_cannot_invent_source_refs(conversation, monkeypatch):
    conv = conversation()
    conv.checkpoint(history=large_history(), strict=True)
    original = conv.history
    generate = conv.runtime.llm.generate

    def wrong_ref(request):
        response = generate(request)
        summary = json.loads(response.text)
        summary["progress"][0]["refs"] = [conv.store.id + "/m999999999"]
        return replace(response, message=Message("assistant", json.dumps(summary)))

    monkeypatch.setattr(conv.runtime.llm, "generate", wrong_ref)
    with pytest.raises(ValueError, match="引用无效|references are invalid"):
        conv.compact()
    assert conv.history == original


def test_parallel_tool_groups_are_retained_as_units(conversation):
    conv = conversation()
    history = large_history()[:-2]
    calls = (ToolCall("a", "read_file", {"path": "a"}), ToolCall("b", "read_file", {"path": "b"}))
    group = (
        Message("assistant", tool_calls=calls),
        Message.tool_result(calls[0], "a"),
        Message.tool_result(calls[1], "b"),
    )
    conv.history = (*history, *group)
    conv.compact()
    assert conv.history[-3:] == group
    LLMRequest(conv.history)


def test_streaming_cancel_at_usage_still_counts_completed_call(conversation, monkeypatch):
    conv = conversation()
    conv.history = large_history()
    cancelled = False

    def stream(request, event):
        nonlocal cancelled
        response = conv.runtime.llm.generate(request)
        cancelled = True
        event("usage", "", 0.1)
        event("end", "", 0.1)
        return response

    def check():
        if cancelled:
            raise KeyboardInterrupt

    monkeypatch.setattr(conv.runtime.llm, "generate_with_events", stream, raising=False)
    conv.runtime.check_cancelled = check
    with pytest.raises(KeyboardInterrupt):
        conv.compact()
    assert conv.status.totals == {"input_tokens": 100, "output_tokens": 20}
    assert conv.history == large_history()


def test_summary_inherits_generation_budget_and_live_thinking_changes(conversation):
    import argparse
    from copy import deepcopy

    from cli.live import ThinkingControl
    from cli.settings import add_runtime_arguments, request_options

    conv = conversation(window=300000)
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--max-output-tokens", "51200", "--thinking-recall", "false"])
    args.provider, args.model = "deepseek", "test"
    runtime = conv.runtime
    runtime.max_output_tokens = args.max_output_tokens
    runtime.temperature = 0.4
    runtime.request_extra = request_options(args)
    thinking = ThinkingControl(runtime, args)
    events = []

    def on_event(event, stats):
        conv.status(event, stats)
        if event == "model_end":
            events.append(deepcopy(stats.model_calls[-1]))

    runtime.on_event = on_event
    for command, expected in [
        ("/thinking on high", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
        ("/thinking off", {"thinking": {"type": "disabled"}}),
        ("/thinking auto", {}),
    ]:
        thinking.command(command)
        conv.history = large_history()
        conv.compaction_state = None
        conv.compact()
        request = runtime.llm.requests[-1]
        assert request.max_output_tokens == 51200
        assert request.extra == expected and request.temperature == 0.4
        assert json.loads(request.messages[-1].content)["summary_token_target"] == 1500
        assert not request.tools and request.tool_choice == "none"
        assert events[-1].thinking == runtime.thinking_settings
        assert events[-1].max_output_tokens == 51200
    assert conv.status.calls == 3


@pytest.mark.parametrize(
    "provider,format_name,native",
    [
        ("deepseek", "chat_completions", {"thinking": {"type": "disabled"}}),
        (
            "deepseek",
            "chat_completions",
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        ("anthropic", "anthropic", {"thinking": {"type": "enabled", "budget_tokens": 8192}}),
        (
            "anthropic",
            "anthropic",
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
        ),
        ("openai", "responses", {"reasoning": {"effort": "high", "summary": "auto"}}),
        ("google", "gemini", {"generationConfig": {"thinkingConfig": {"thinkingBudget": 8192}}}),
        ("qwen", "chat_completions", {"enable_thinking": True, "thinking_budget": 8192}),
    ],
)
def test_native_thinking_controls_reach_provider_payload(
    conversation, provider, format_name, native
):
    from copy import deepcopy

    from llm.adapters import ADAPTERS

    conv = conversation(window=300000)
    runtime = conv.runtime
    runtime.max_output_tokens = 51200
    runtime.request_extra = deepcopy(native)
    runtime.request_extra.update(response_format={"type": "json_schema", "schema": {}}, stop=["}"])
    if "generationConfig" in runtime.request_extra:
        runtime.request_extra["generationConfig"].update(responseSchema={}, stopSequences=["}"])
    if "output_config" in runtime.request_extra:
        runtime.request_extra["output_config"]["format"] = {"type": "json_schema", "schema": {}}
    before = deepcopy(runtime.request_extra)
    conv.history = large_history()
    conv.compact()
    request = runtime.llm.requests[-1]
    assert request.extra == native
    body = ADAPTERS[format_name](provider, "test").encode(request)
    for key, value in native.items():
        if key == "generationConfig":
            assert body[key]["thinkingConfig"] == value["thinkingConfig"]
            assert body[key]["maxOutputTokens"] == 51200
        else:
            assert body[key] == value
    assert runtime.request_extra == before
    # Provider encoding and request consumers must not mutate live settings.
    request.extra.clear()
    assert runtime.request_extra == before


def test_reasoning_can_exceed_summary_target_without_truncating_request(conversation, monkeypatch):
    conv = conversation(window=300000)
    conv.history = large_history()
    conv.runtime.max_output_tokens = 51200
    conv.runtime.request_extra = {"thinking": {"type": "enabled"}}
    generate = conv.runtime.llm.generate

    def with_reasoning(request):
        response = generate(request)
        if request.max_output_tokens <= 3000:
            return replace(
                response,
                message=Message("assistant", ""),
                finish_reason="length",
                usage=Usage(81161, 3000, reasoning_tokens=3000),
            )
        return replace(response, usage=Usage(81161, 4200, reasoning_tokens=4000))

    monkeypatch.setattr(conv.runtime.llm, "generate", with_reasoning)
    conv.compact()
    assert conv.compaction_state is not None
    assert conv.status.totals == {"input_tokens": 81161, "output_tokens": 4200}


def test_length_error_reports_actual_limit_and_reasoning_usage(conversation, monkeypatch):
    conv = conversation()
    conv.history = large_history()
    original = conv.history
    generate = conv.runtime.llm.generate

    def limited(request):
        response = generate(request)
        return replace(
            response,
            message=Message("assistant", ""),
            finish_reason="length",
            usage=Usage(100, 999, reasoning_tokens=999),
        )

    monkeypatch.setattr(conv.runtime.llm, "generate", limited)
    with pytest.raises(ValueError, match=r"1000 tokens; reported output=999, reasoning=999"):
        conv.compact()
    assert conv.history == original and len(conv.runtime.llm.requests) == 1
    assert conv.status.totals["output_tokens"] == 999


def test_summary_input_reserves_configured_output_and_chunks_fit(conversation):
    from llm.token_estimation import estimate_context_tokens

    conv = conversation(window=20000)
    conv.runtime.max_output_tokens = 8192
    conv.history = large_history()
    conv.compact()
    assert len(conv.runtime.llm.requests) > 1
    for request in conv.runtime.llm.requests:
        assert request.max_output_tokens == 8192
        assert estimate_context_tokens(request.messages) + request.max_output_tokens <= 20000
        assert json.loads(request.messages[-1].content)["summary_token_target"] < 1500


def test_large_output_allowance_does_not_relax_final_summary_size(conversation, monkeypatch):
    conv = conversation(window=300000)
    conv.history = large_history()
    original = conv.history
    conv.runtime.max_output_tokens = 51200
    generate = conv.runtime.llm.generate

    def verbose(request):
        response = generate(request)
        summary = json.loads(response.text)
        summary["progress"][0]["text"] = "too much detail " * 1200
        return replace(response, message=Message("assistant", json.dumps(summary)))

    monkeypatch.setattr(conv.runtime.llm, "generate", verbose)
    with pytest.raises(ValueError, match="exceeds AGENT_COMPACT_SUMMARY_TOKENS"):
        conv.compact()
    assert conv.history == original


def test_compaction_follows_model_switch_without_old_provider_options(conversation):
    from cli.models import ModelControl, ModelSelection

    conv = conversation(window=300000)
    old_model = conv.runtime.llm
    conv.runtime.request_extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}

    def factory(config):
        model = Model()
        model.config = config
        return model

    control = ModelControl(conv.runtime, old_model.config, client_factory=factory)
    control.switch(ModelSelection("qwen", "qwen-plus", "synthetic-key"))
    conv.history = large_history()
    conv.compact()
    assert old_model.requests == []
    assert conv.runtime.llm.config.provider == "qwen"
    assert conv.runtime.llm.requests[-1].extra == {}


def test_summary_uses_input_only_window_without_subtracting_output(conversation):
    from llm.token_estimation import estimate_context_tokens

    conv = conversation(window=20000)
    conv.status.context_limit_kind = "input"
    conv.runtime.max_output_tokens = 51200
    conv.history = large_history()
    conv.compact()
    for request in conv.runtime.llm.requests:
        assert request.max_output_tokens == 51200
        assert estimate_context_tokens(request.messages) <= 20000
