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
from agent.conversation import SavedConversation
from agent.history import HistoryArchive, HistoryTool
from agent.session import SessionStore
from cli.session_status import SessionStatus
from cli.terminal.application import ConversationUI
from llm import LLMConfig, LLMRequest, LLMResponse, Message, ToolCall, Usage
from llm.schemas import ProviderState
from tools import ExecutionKind, ToolResult


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
            or CompactionSettings(auto=False, threshold=0.55, target=0.20, keep_tokens=512),
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
    assert conv.status.totals == {"input_tokens": 200, "output_tokens": 40}
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]
    assert conv.store.data["status"]["calls"] == 2


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
    conv = conversation(settings=CompactionSettings(auto=True, keep_tokens=512))

    class LargeTool:
        execution_kind = ExecutionKind.HOST_CONTROL
        scheduling_policy = HistoryTool.scheduling_policy
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
        dict(max_refinements=5),
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


def set_summary_sizes(conv, monkeypatch, sizes):
    """Supply summary lengths while using the actual archive references and estimator."""
    generate = conv.runtime.llm.generate
    sizes = iter(sizes)

    def response(request):
        result = generate(request)
        size = next(sizes)
        if isinstance(size, BaseException):
            raise size
        summary = json.loads(result.text)
        summary["progress"][0]["text"] = "detail " * size
        return replace(result, message=Message("assistant", json.dumps(summary)))

    monkeypatch.setattr(conv.runtime.llm, "generate", response)


def test_catalog_policy_ignores_session_settings_and_tracks_usage(conversation):
    from copy import deepcopy

    conv = conversation(window=300000)
    runtime = conv.runtime
    runtime.llm.config = LLMConfig("deepseek", "deepseek-flash", api_key="fake")
    runtime.max_output_tokens = 51200
    runtime.temperature = 0.4
    runtime.request_extra = {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "stop": ["}"],
        "response_format": {"type": "json_object"},
    }
    previous = deepcopy(runtime.request_extra)
    records = []

    def event(name, stats):
        conv.status(name, stats)
        if name == "model_end":
            records.append(deepcopy(stats.model_calls[-1]))

    runtime.on_event = event
    conv.history = large_history()
    conv.compact()
    request = runtime.llm.requests[-1]
    assert request.max_output_tokens == 32768
    assert request.extra == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    assert request.temperature is None and not request.tools and request.tool_choice == "none"
    assert runtime.request_extra == previous and runtime.max_output_tokens == 51200
    assert records[-1].thinking == {"mode": "enabled", "effort": "low"}
    assert records[-1].max_output_tokens == 32768
    assert conv.status.totals == {"input_tokens": 100, "output_tokens": 20}


@pytest.mark.parametrize("truncate_count", [1, 3, 99])
def test_truncation_increases_catalog_budget_with_bounded_retries(
    conversation, monkeypatch, truncate_count
):
    conv = conversation(window=1000000)
    conv.runtime.llm.config = LLMConfig("deepseek", "deepseek-flash", api_key="fake")
    conv.history = large_history()
    generate = conv.runtime.llm.generate

    def generate_truncated(request):
        response = generate(request)
        if len(conv.runtime.llm.requests) <= truncate_count:
            return replace(
                response,
                finish_reason="length",
                message=Message("assistant", ""),
                usage=Usage(
                    100, request.max_output_tokens, reasoning_tokens=request.max_output_tokens
                ),
            )
        return response

    monkeypatch.setattr(conv.runtime.llm, "generate", generate_truncated)
    if truncate_count == 99:
        with pytest.raises(ValueError, match="摘要输出截断"):
            conv.compact()
        assert conv.history == large_history()
    else:
        conv.compact()
    requests = conv.runtime.llm.requests
    expected = [32768, 65536, 131072, 262144, 393216][: min(truncate_count + 1, 5)]
    assert [r.max_output_tokens for r in requests] == expected
    assert conv.status.calls == len(expected)
    assert conv.status.totals["output_tokens"] == sum(expected[:truncate_count]) + (
        20 if truncate_count < len(expected) else 0
    )


def test_unknown_output_cap_is_not_increased(conversation, monkeypatch):
    conv = conversation()
    conv.history = large_history()
    generate = conv.runtime.llm.generate

    def truncated(request):
        return replace(
            generate(request), finish_reason="length", usage=Usage(100, 999, reasoning_tokens=999)
        )

    monkeypatch.setattr(conv.runtime.llm, "generate", truncated)
    with pytest.raises(ValueError, match="8192 tokens.*输出=999.*思考=999"):
        conv.compact()
    assert len(conv.runtime.llm.requests) == 1 and conv.history == large_history()
    assert conv.status.totals["output_tokens"] == 999


@pytest.mark.parametrize("kind,window", [("context", 20000), ("context", 8000), ("input", 20000)])
def test_independent_requests_fit_window_and_ignore_session_output(conversation, kind, window):
    from llm.token_estimation import estimate_context_tokens

    conv = conversation(window=window)
    conv.status.context_limit_kind = kind
    conv.runtime.max_output_tokens = 51200 if kind == "input" else 1000
    conv.history = large_history()
    conv.compact()
    for request in conv.runtime.llm.requests:
        occupied = estimate_context_tokens(request.messages)
        if kind == "context":
            occupied += request.max_output_tokens
        assert occupied <= window - conv.compactor.headroom(window)


def test_large_summary_has_no_independent_hard_limit(conversation, monkeypatch):
    conv = conversation(window=300000)
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, [2500])
    conv.compact()
    assert conv.compaction_state["after"] > 3000
    assert conv.compaction_state["target_met"]
    assert len(conv.runtime.llm.requests) == 1


@pytest.mark.parametrize(
    "sizes,expected_calls,target_met",
    [
        ([3000, 100], 2, True),
        ([3000, 3000, 3000], 3, False),
        ([3000, 4000, 5000], 3, False),
        ([3000, ValueError("invalid summary")], 2, False),
    ],
)
def test_bounded_refinement_adopts_best_safe_result(
    conversation, monkeypatch, sizes, expected_calls, target_met
):
    conv = conversation()
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, sizes)
    notice = conv.compact()
    state = conv.compaction_state
    assert state["target_met"] is target_met
    assert state["after"] < state["before"] and state["after"] <= state["safety_limit"]
    assert len(conv.runtime.llm.requests) == expected_calls
    assert ("未达到目标" in notice) is not target_met
    summary = json.loads(conv.history[2].content.split("\n", 1)[1])
    assert len(summary["progress"][0]["text"]) == (100 if target_met else 3000) * 7
    for request in conv.runtime.llm.requests[1:]:
        payload = json.loads(request.messages[-1].content)
        assert payload["refining"]
        assert [item["ref"] for item in payload["records"]] == [
            f"R{i}" for i in range(1, len(payload["records"]) + 1)
        ]
    conv.archive.validate_refs(
        [ref for items in summary.values() for item in items for ref in item["refs"]]
    )


@pytest.mark.parametrize("size", [18000, 5850])
def test_rejects_unsafe_or_insufficient_reduction(conversation, monkeypatch, size):
    conv = conversation(settings=CompactionSettings(auto=False, keep_tokens=512, max_refinements=0))
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, [size])
    with pytest.raises(ValueError, match="无法采用压缩结果"):
        conv.compact()
    assert conv.history == large_history() and conv.compaction_state is None
    assert conv.archive.search("ORIGINAL_MARKER")["matches"]


def test_cancel_during_refinement_preserves_original_and_usage(conversation, monkeypatch):
    conv = conversation()
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, [3000, KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        conv.compact()
    assert conv.history == large_history() and conv.compaction_state is None
    assert conv.status.totals["output_tokens"] == 20


def test_over_target_state_restores_and_delays_auto_compaction(conversation, monkeypatch):
    settings = CompactionSettings(
        auto=True, target=0.05, threshold=0.1, keep_tokens=512, max_refinements=0
    )
    conv = conversation(settings=settings)
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, [2000])
    conv.compact()
    state = conv.compaction_state.copy()
    conv.store.close()
    restored = conversation(settings=settings)
    assert restored.compaction_state == json.loads(json.dumps(state)) and not state["target_met"]
    assert restored.compactor.before_request(restored.history, 1000) is restored.history
    assert restored.runtime.llm.requests == []
    grown = [*restored.history, Message("assistant", "more " * 2000)]
    restored.compactor.before_request(grown, 1000)
    assert restored.runtime.llm.requests


def test_auto_delay_never_bypasses_safety_limit(conversation, monkeypatch):
    conv = conversation(
        settings=CompactionSettings(
            auto=True, target=0.05, threshold=0.1, keep_tokens=512, max_refinements=0
        )
    )
    conv.history = large_history()
    conv.compact()
    conv.compaction_state["auto_retry_at"] = 1000000
    called = []
    monkeypatch.setattr(conv.compactor, "compact", lambda *a, **k: called.append(True))
    conv.compactor.before_request([*conv.history, Message("assistant", "data " * 20000)], 1000)
    assert called


def test_old_summary_configuration_is_ignored():
    import argparse

    from cli.config_command import validate_values
    from cli.settings import add_runtime_arguments

    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--compact-summary-tokens", "1", "--compact-max-refinements", "0"])
    assert args.compact_max_refinements == 0
    validate_values({"AGENT_COMPACT_SUMMARY_TOKENS": "1", "AGENT_COMPACT_MAX_REFINEMENTS": "0"})
    with pytest.raises(ValueError):
        validate_values({"AGENT_COMPACT_MAX_REFINEMENTS": "5"})


def test_compaction_follows_model_switch_without_old_provider_options(conversation):
    from cli.models import ModelControl, ModelSelection
    from llm.independent import IndependentRequestPolicy

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
    request = conv.runtime.llm.requests[-1]
    policy = IndependentRequestPolicy.from_config(conv.runtime.llm.config)
    assert not old_model.requests and request.extra == policy.extras(request.max_output_tokens)


def test_retry_respects_reported_input_and_leaves_window_headroom(conversation, monkeypatch):
    conv = conversation(window=100000)
    conv.runtime.llm.config = LLMConfig("deepseek", "deepseek-flash", api_key="fake")
    conv.history = large_history()
    generate = conv.runtime.llm.generate

    def truncated_once(request):
        response = generate(request)
        return replace(
            response,
            usage=Usage(50000, 50),
            finish_reason="length" if len(conv.runtime.llm.requests) == 1 else "stop",
        )

    monkeypatch.setattr(conv.runtime.llm, "generate", truncated_once)
    conv.compact()
    assert [r.max_output_tokens for r in conv.runtime.llm.requests] == [32768, 45000]
    assert conv.status.totals["input_tokens"] == 100000


def test_legacy_compaction_state_is_valid_and_new_fields_are_checked(conversation):
    from agent.session import validate_compaction

    conv = conversation()
    conv.history = large_history()
    conv.compact()
    state = conv.compaction_state
    history = [m.to_dict() for m in conv.history]
    legacy = {key: state[key] for key in ("snapshot", "pins", "prefix", "before", "after")}
    validate_compaction(legacy, history)
    for update in [
        {"target_met": not state["target_met"]},
        {"safety_limit": state["after"] - 1},
        {"auto_retry_at": state["after"]},
        {"target": True},
    ]:
        with pytest.raises(ValueError, match="压缩元数据"):
            validate_compaction({**state, **update}, history)


def test_compaction_trace_contains_target_adoption_and_policy(conversation, tmp_path, monkeypatch):
    from agent.Tracing import Tracer

    conv = conversation(
        settings=CompactionSettings(auto=False, target=0.1, keep_tokens=512, max_refinements=0)
    )
    conv.history = large_history()
    set_summary_sizes(conv, monkeypatch, [3000])
    logs = tmp_path / "traces"
    with Tracer(logs, provider="deepseek", model="test") as tracer:

        def event(name, stats):
            conv.status(name, stats)
            tracer(name, stats)

        conv.runtime.on_event = event
        conv.compact()
    entries = [
        json.loads(line) for path in logs.glob("*.jsonl") for line in path.read_text().splitlines()
    ]
    completed = next(e["compaction"] for e in entries if e["event"] == "compaction_end")
    assert not completed["target_met"] and completed["after"] > completed["target"]
    call = next(e["model_call"] for e in entries if e["event"] == "model_end")
    assert call["thinking"] == {"mode": "auto"} and call["max_output_tokens"] == 8192


def test_safety_check_overrides_a_high_automatic_threshold(conversation, monkeypatch):
    conv = conversation(settings=CompactionSettings(auto=True, threshold=0.99))
    budget = conv.compactor.input_budget(1000)
    monkeypatch.setattr(
        conv.runtime,
        "estimate_context_tokens",
        lambda _: budget - conv.compactor.headroom(budget) + 1,
    )
    called = []
    monkeypatch.setattr(conv.compactor, "compact", lambda *a, **k: called.append(True))
    conv.compactor.before_request(large_history(), 1000)
    assert called


# Review F2: content deduplication must not deduplicate user-turn occurrences.
def correction_history(*instructions):
    messages = [Message("system", "你是编码助手")]
    for instruction in instructions:
        messages.extend(
            (Message("user", instruction), Message("assistant", "history line\n" * 1000))
        )
    messages.extend((Message("user", "继续验证"), Message("assistant", "近期状态")))
    return tuple(messages)


def correction_pins(conv):
    return [
        p for p in conv.compaction_state["pins"] if p["text"] in {"使用中文回答", "改用英文回答"}
    ]


def test_repeated_user_correction_keeps_occurrences_and_order(conversation):
    conv = conversation()
    conv.history = correction_history("使用中文回答", "改用英文回答", "使用中文回答")
    conv.compact()
    pins = correction_pins(conv)
    assert [p["text"] for p in pins] == ["使用中文回答", "改用英文回答", "使用中文回答"]
    assert pins[0]["ref"] == pins[2]["ref"]
    assert len({p["occurrence"] for p in pins}) == 3
    with conv.archive.connect() as db:
        for pin in pins:
            snapshot, position = pin["occurrence"].split(":")
            ids = json.loads(
                db.execute("SELECT message_ids FROM snapshots WHERE id=?", (snapshot,)).fetchone()[
                    0
                ]
            )
            assert conv.archive.ref(ids[int(position)]) == pin["ref"]
    originals = json.loads(conv.history[1].content.split("\n", 1)[1])["user_originals"]
    assert [p["text"] for p in originals][:3] == [p["text"] for p in pins]


@pytest.mark.parametrize("restart", [False, True])
def test_new_repeated_correction_survives_multiple_compactions(conversation, restart):
    conv = conversation()
    conv.history = correction_history("使用中文回答", "改用英文回答")
    conv.compact()
    original = [p.copy() for p in correction_pins(conv)]
    if restart:
        conv.store.close()
        conv = conversation()
    conv.history += correction_history("使用中文回答")[1:]
    conv.compact()
    pins = correction_pins(conv)
    assert pins[:2] == original
    assert [p["text"] for p in pins] == ["使用中文回答", "改用英文回答", "使用中文回答"]
    assert pins[0]["ref"] == pins[2]["ref"]
    assert len({p["occurrence"] for p in pins}) == 3
    # Another compaction must not append the already pinned occurrences again.
    conv.history += (Message("assistant", "another result\n" * 2000), Message("assistant", "近期"))
    conv.compact()
    assert correction_pins(conv) == pins


def test_oversized_tail_does_not_pin_retained_user_twice(conversation):
    conv = conversation()
    long_reply = Message("assistant", "large result\n" * 3000)
    conv.history = (Message("system", "你是编码助手"), Message("user", "使用中文回答"), long_reply)
    conv.compact()
    assert conv.compaction_state["pins"] == []
    assert conv.history[-1] == Message("user", "使用中文回答")
    conv.store.close()
    conv = conversation()
    conv.history += (long_reply,)
    conv.compact()
    assert conv.compaction_state["pins"] == []
    conv.history += (Message("user", "改用英文回答"), long_reply)
    conv.compact()
    assert [p["text"] for p in correction_pins(conv)] == ["使用中文回答"]
    conv.history += (Message("user", "使用中文回答"), long_reply)
    conv.compact()
    assert [p["text"] for p in correction_pins(conv)] == ["使用中文回答", "改用英文回答"]
    assert conv.history[-1] == Message("user", "使用中文回答")


def test_pin_occurrences_validate_and_legacy_pins_remain_readable(conversation):
    from copy import deepcopy

    from agent.session import validate_compaction

    conv = conversation()
    conv.history = correction_history("使用中文回答", "改用英文回答", "使用中文回答")
    conv.compact()
    history = [m.to_dict() for m in conv.history]
    state = conv.compaction_state
    for occurrence in (
        None,
        "bad:1",
        "a" * 32 + ":-1",
        "a" * 32 + ":01",
        state["pins"][1]["occurrence"],
    ):
        broken = deepcopy(state)
        broken["pins"][0]["occurrence"] = occurrence
        with pytest.raises(ValueError, match="压缩元数据"):
            validate_compaction(broken, history)
    legacy = deepcopy(state)
    for pin in legacy["pins"]:
        del pin["occurrence"]
    validate_compaction(legacy, history)


# Review F3: crossing a compaction threshold is not the same as exceeding capacity.
@pytest.mark.parametrize("threshold", [0.55, 0.75])
def test_first_long_input_runs_without_a_summary_when_it_fits(conversation, threshold):
    settings = CompactionSettings(auto=True, threshold=threshold)
    conv = conversation(settings=settings)
    task = "long input " * 6500
    original = (Message("system", conv.runtime.system_prompt), Message("user", task))
    size = conv.runtime.estimate_context_tokens(original)
    budget = conv.compactor.input_budget()
    assert budget * threshold < size <= budget - conv.compactor.headroom(budget)
    conv.start_task(task)
    result = conv.runtime.run(task, history=conv.history)
    conv.finish_task(result)
    assert result.status == "completed"
    assert len(conv.runtime.llm.requests) == 1
    assert conv.runtime.llm.requests[0].tool_choice != "none"
    assert conv.runtime.llm.requests[0].messages[-1].content == task
    assert conv.compaction_state is None and not conv.archive.path.exists()
    assert conv.history[-2].content == task


def test_first_input_over_safety_budget_gets_actionable_error(conversation):
    conv = conversation(settings=CompactionSettings(auto=True))
    task = "long input " * 8500
    conv.start_task(task)
    with pytest.raises(ValueError, match="超过安全输入上限.*没有可压缩.*拆分输入"):
        conv.runtime.run(task, history=conv.history)
    assert not conv.runtime.llm.requests
    assert conv.history[-1].content == task


def test_auto_keeps_recent_history_when_there_is_nothing_to_summarize(conversation):
    conv = conversation(settings=CompactionSettings(auto=True, threshold=0.1, target=0.05))
    messages = (
        Message("system", "fixed instructions " * 600),
        Message("user", "任务"),
        Message("assistant", "回复"),
    )
    assert conv.compactor.before_request(messages, 1000) is messages
    assert not conv.runtime.llm.requests


@pytest.mark.parametrize("failure", [ValueError("corrupt archive"), OSError("disk full")])
def test_auto_does_not_hide_archive_or_storage_errors(conversation, monkeypatch, failure):
    conv = conversation(settings=CompactionSettings(auto=True, threshold=0.3, target=0.2))

    def fail(_):
        raise failure

    monkeypatch.setattr(conv.archive, "archive", fail)
    with pytest.raises(type(failure), match=str(failure)):
        conv.compactor.before_request(large_history(), 1000)
    assert not conv.runtime.llm.requests and conv.history == large_history()


def test_manual_noop_is_explicit_and_does_not_reuse_previous_success(conversation):
    from agent.compaction import CompactionNotNeeded

    conv = conversation()
    conv.history = (Message("user", "任务"),)
    with pytest.raises(CompactionNotNeeded, match="无需压缩"):
        conv.compact()
    assert conv.compaction_state is None and not conv.runtime.llm.requests


def test_cli_compaction_defaults_come_from_compaction_settings():
    from cli.settings import RUNTIME_OPTIONS

    defaults = CompactionSettings()
    fields = {
        "auto-compact": "auto",
        "compact-threshold": "threshold",
        "compact-target": "target",
        "compact-keep-tokens": "keep_tokens",
        "compact-max-refinements": "max_refinements",
    }
    for flag, _key, value, _kind in RUNTIME_OPTIONS:
        if flag in fields:
            assert value == getattr(defaults, fields[flag])
    assert defaults.threshold == 0.75 and defaults.target == 0.45


def test_legacy_pins_restore_and_accept_a_new_identical_correction(conversation):
    from agent.history import encoded

    conv = conversation()
    conv.history = correction_history("使用中文回答", "改用英文回答")
    conv.compact()
    # Construct the old on-disk representation, including its matching prefix.
    state = conv.compaction_state
    for pin in state["pins"]:
        del pin["occurrence"]
    heading = conv.history[1].content.split("\n", 1)[0]
    intro = Message("user", heading + "\n" + encoded({"user_originals": state["pins"]}))
    conv.history = (conv.history[0], intro, *conv.history[2:])
    state["prefix"][0] = intro.to_dict()
    conv.checkpoint(strict=True)
    conv.store.close()
    restored = conversation()
    restored.history += correction_history("使用中文回答")[1:]
    restored.compact()
    pins = correction_pins(restored)
    assert [p["text"] for p in pins] == ["使用中文回答", "改用英文回答", "使用中文回答"]
    assert "occurrence" not in pins[0] and "occurrence" in pins[-1]


def test_auto_noop_does_not_suppress_checkpoint_failure(conversation, monkeypatch):
    conv = conversation(settings=CompactionSettings(auto=True))

    def fail(_):
        raise OSError("cannot persist input")

    monkeypatch.setattr(conv.store, "save", fail)
    with pytest.raises(OSError, match="会话保存失败"):
        # The session layer wraps the storage error; automatic compaction must propagate it.
        conv.runtime.run("long input " * 6500)
    assert not conv.runtime.llm.requests
