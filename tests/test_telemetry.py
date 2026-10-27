import json
from copy import deepcopy

import pytest

from agent import AgentRuntime
from agent.skills import SkillRegistry
from agent.Tracing import Tracer
from llm import LLMResponse, LLMTimeoutError, Message, ToolCall, Usage
from tools import DEFAULT_TOOL_GROUPS, ExecutionKind, ReadFileTool, WriteFileTool


@pytest.fixture
def tracer(tmp_path):
    with Tracer(tmp_path / "logs") as writer:
        yield writer


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)

    def generate(self, request):
        item = next(self.responses)
        if isinstance(item, BaseException):
            raise item
        return item


def reply(*, calls=(), usage=None, finish=None):
    return LLMResponse(
        "test",
        "test",
        Message("assistant", "done", calls),
        finish or ("tool_calls" if calls else "stop"),
        usage if usage is not None else Usage(),
    )


def test_usage_accumulates_only_current_task_and_preserves_subtotals(tmp_path):
    runtime = AgentRuntime(
        Model(
            [
                reply(
                    calls=[ToolCall("w", "write_file", {"path": "a", "content": "private text"})],
                    usage=Usage(
                        input_tokens=100,
                        output_tokens=20,
                        cached_input_tokens=50,
                        reasoning_tokens=10,
                    ),
                ),
                reply(
                    usage=Usage(
                        input_tokens=200,
                        output_tokens=30,
                        total_tokens=230,
                        cached_input_tokens=0,
                        reasoning_tokens=0,
                    )
                ),
                reply(usage=Usage(input_tokens=10, output_tokens=5)),
            ]
        ),
        [WriteFileTool(tmp_path)],
    )
    first = runtime.run("write")
    assert first.stats.token_total("total_tokens") == (350, 2)
    assert first.stats.token_total("input_tokens") == (300, 2)
    assert first.stats.token_total("reasoning_tokens") == (10, 2)
    assert len(first.stats.model_calls) == 2
    assert first.stats.tool_calls[0].status == "success"
    assert first.stats.elapsed_seconds > 0
    assert first.stats.model_calls[0].elapsed_seconds > 0
    assert "private text" not in str(first.stats)
    second = runtime.run("continue", history=first.history)
    assert second.stats.task_number == 2
    assert second.stats.token_total("total_tokens") == (15, 1)
    assert len(second.stats.tool_calls) == 0
    assert first.stats.token_total("total_tokens") == (350, 2)


@pytest.mark.parametrize(
    "error,status",
    [
        (LLMTimeoutError("no response"), "failed"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
def test_failure_still_logs_partial_usage_and_total_time(tmp_path, capsys, error, status, tracer):
    runtime = AgentRuntime(
        Model(
            [
                reply(
                    calls=[ToolCall("r", "read_file", {"reads": [{"path": "missing"}]})],
                    usage=Usage(input_tokens=20, output_tokens=4),
                ),
                error,
            ]
        ),
        [ReadFileTool(tmp_path)],
        on_event=tracer,
    )
    with pytest.raises(type(error)):
        runtime.run("read")
    stats = runtime.last_stats
    assert stats.status == status
    assert len(stats.model_calls) == 2
    assert stats.token_total("total_tokens") == (24, 1)
    assert stats.tool_calls[0].error_code == "READ_FAILED"
    assert capsys.readouterr().out == ""
    output = tracer.text_path.read_text()
    assert "仅 1/2 次已知" in output
    assert "总耗时=" in output
    assert "READ_FAILED" in output
    assert "思考=未返回" in output


@pytest.mark.parametrize("finish,status", [("tool_calls", "max_steps"), ("length", "max_steps")])
def test_stopped_tasks_emit_final_statistics(finish, status):
    events = []
    calls = [ToolCall("unknown", "unknown_tool", {})] if finish == "tool_calls" else []
    runtime = AgentRuntime(
        Model([reply(calls=calls, finish=finish)]),
        max_steps=1,
        on_event=lambda event, stats: events.append((event, stats)),
    )
    result = runtime.run("task")
    assert result.status == status
    assert events[-1][0] == "task_end"
    assert events[-1][1].status == status
    assert result.stats.token_total("total_tokens") == (None, 0)


def test_tool_interrupt_and_broken_logger_do_not_lose_file_operation(tmp_path):
    class InterruptedReader(ReadFileTool):
        execution_kind = ExecutionKind.TRUSTED_FILE

        def execute(self, arguments):
            raise KeyboardInterrupt

    runtime = AgentRuntime(
        Model([reply(calls=[ToolCall("r", "read_file", {"reads": [{"path": "a"}]})])]),
        [InterruptedReader(tmp_path)],
    )
    with pytest.raises(KeyboardInterrupt):
        runtime.run("read")
    assert runtime.last_stats.tool_calls[0].status == "interrupted"
    assert runtime.last_stats.tool_calls[0].elapsed_seconds > 0

    def broken_sink(event, stats):
        stats.status = "modified"
        raise OSError("log unavailable")

    runtime = AgentRuntime(
        Model(
            [
                reply(calls=[ToolCall("w", "write_file", {"path": "a", "content": "ok"})]),
                reply(),
            ]
        ),
        [WriteFileTool(tmp_path)],
        on_event=broken_sink,
    )
    result = runtime.run("write")
    assert result.stats.status == "completed"
    assert (tmp_path / "a").read_text() == "ok"


def test_zero_usage_is_reported_as_zero(capsys, tracer):
    runtime = AgentRuntime(
        Model([reply(usage=Usage(input_tokens=0, output_tokens=0))]), on_event=tracer
    )
    result = runtime.run("task")
    snapshot = deepcopy(result.stats)
    assert snapshot.token_total("total_tokens") == (0, 1)
    assert capsys.readouterr().out == ""
    assert "输入=0，输出=0，合计=0" in tracer.text_path.read_text().replace(",", "，").replace(
        " ", ""
    )


def test_structured_trace_ids_usage_permissions_and_session_summary(tmp_path, capsys):
    with Tracer(
        tmp_path / "logs", provider="test", model="model", workspace=str(tmp_path)
    ) as writer:
        runtime = AgentRuntime(
            Model(
                [
                    reply(usage=Usage(input_tokens=10, output_tokens=2)),
                    reply(usage=Usage(input_tokens=20, output_tokens=3)),
                ]
            ),
            on_event=writer,
        )
        first = runtime.run("one")
        runtime.run("two", history=first.history)
        # Events reach disk before session close.
        assert '"event": "task_end"' in writer.jsonl_path.read_text()
    events = [json.loads(line) for line in writer.jsonl_path.read_text().splitlines()]
    assert {e["session_id"] for e in events} == {writer.session_id}
    tasks = [e for e in events if e["event"] == "task_end"]
    assert len({e["task_id"] for e in tasks}) == 2
    assert events[0]["event"] == "session_start"
    end = events[-1]
    assert end["event"] == "session_end"
    assert end["tasks"] == 2 and end["task_statuses"]["completed"] == 2
    assert end["model_calls"] == 2 and end["tool_calls"] == 0
    assert end["usage"]["total_tokens"]["known_total"] == 35
    assert end["usage"]["total_tokens"]["complete"] is True
    assert end["task_elapsed_seconds"] <= end["elapsed_seconds"]
    for path in (writer.text_path, writer.jsonl_path):
        assert path.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""
    original = writer.text_path.read_text()
    with pytest.raises(FileExistsError), Tracer(tmp_path / "logs", session_id=writer.session_id):
        pass
    assert writer.text_path.read_text() == original


def test_disk_write_failure_does_not_repeat_file_operations(tmp_path, capsys):
    class BrokenFile:
        def write(self, text):
            raise OSError("disk full")

        def close(self):
            pass

    with Tracer(tmp_path / "logs") as writer:
        writer._files[0].close()
        writer._files[0] = BrokenFile()
        runtime = AgentRuntime(
            Model(
                [
                    reply(
                        calls=[
                            ToolCall("w", "write_file", {"path": "a", "content": "private text"})
                        ]
                    ),
                    reply(),
                ]
            ),
            [WriteFileTool(tmp_path)],
            on_event=writer,
        )
        result = runtime.run("write")
    assert writer.error == "OSError"
    assert result.status == "completed"
    assert len(result.stats.tool_calls) == 1
    assert (tmp_path / "a").read_text() == "private text"
    assert capsys.readouterr().out == ""


def test_trace_exposes_command_exit_without_leaking_output(tracer):
    from agent.Tracing import RunTrace, ToolCallRecord
    from tools import ToolResult

    call = ToolCall("command-id", "run_command", {"command": ["test"]})
    record = ToolCallRecord(1, call.id, call.name, {})
    message = ToolResult(
        True,
        {"exit_code": 2, "timed_out": False, "cleanup_error": None, "stderr": "private output"},
    ).to_message(call)
    RunTrace.tool_result(record, message)
    assert record.status == "success"  # Invocation succeeded; the command exited nonzero.
    assert record.exit_code == 2
    assert "private output" not in str(record)
    from agent.Tracing import RunStats

    stats = RunStats(1, tool_calls=[record])
    tracer("tool_end", stats)
    event = json.loads(tracer.jsonl_path.read_text().splitlines()[-1])
    assert event["tool_call"]["exit_code"] == 2
    assert "命令退出码=2" in tracer.text_path.read_text()


def test_per_call_text_log_includes_reasoning_subtotal(tracer):
    runtime = AgentRuntime(
        Model([reply(usage=Usage(input_tokens=6687, output_tokens=48304, reasoning_tokens=44963))]),
        on_event=tracer,
    )
    runtime.run("task")
    line = next(
        line for line in tracer.text_path.read_text().splitlines() if "模型调用 #1 success" in line
    )
    assert "输入=6687, 输出=48304, 其中思考=44963" in line
    assert runtime.last_stats.token_total("total_tokens")[0] == 54991


@pytest.mark.parametrize(
    "tool_name, key, requested_name, expected_status",
    [
        ("load_skill", "name", "debug-and-fix", "success"),
        ("load_skill", "name", "missing-skill", "failed"),
        ("load_skill", "name", '未知-技能"' + "x" * 310, "failed"),
        ("load_tool_group", "group", "file_editing", "success"),
        ("load_tool_group", "group", "missing_group", "failed"),
        ("load_tool_group", "group", '未知-分组"\n' + "x" * 310, "failed"),
    ],
)
def test_load_names_are_preserved_in_all_trace_logs(
    tmp_path, open_conversation, tool_name, key, requested_name, expected_status
):
    conversation = open_conversation()
    store = conversation.store
    arguments = {key: requested_name}
    call = ToolCall("load-call", tool_name, deepcopy(arguments))
    with Tracer(tmp_path / "run-logs", session_store=store) as writer:
        runtime = AgentRuntime(
            Model([reply(calls=[call]), reply()]),
            [WriteFileTool(store.project)],
            tool_groups=DEFAULT_TOOL_GROUPS,
            skills=SkillRegistry(store.project),
            on_event=writer,
        )
        result = runtime.run("加载指定技能")
    assert call.arguments == arguments
    assert result.stats.tool_calls[0].arguments == arguments
    for path in (writer.jsonl_path, store.catalog.log_path(store.id, "jsonl")):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        calls = [
            event["tool_call"] for event in events if event["event"] in {"tool_start", "tool_end"}
        ]
        assert len(calls) == 2
        assert all(record["arguments"] == arguments for record in calls)
        assert calls[-1]["status"] == expected_status
    for path in (writer.text_path, store.catalog.log_path(store.id, "trace")):
        text = path.read_text()
        assert "参数=" + json.dumps(arguments, ensure_ascii=False) in text
        assert "省略正文" not in text


def test_skill_name_exception_does_not_expose_other_arguments(tmp_path, tracer):
    private_content = "private source or extra argument"
    private_name = "private unrelated tool name parameter"
    calls = [
        ToolCall("invalid-skill", "load_skill", {"name": "coding", "content": private_content}),
        ToolCall(
            "invalid-write",
            "write_file",
            {
                "path": "unused.txt",
                "name": private_name,
                "content": private_content,
            },
        ),
    ]
    original = deepcopy(calls)
    result = AgentRuntime(
        Model([reply(calls=calls), reply()]),
        [WriteFileTool(tmp_path)],
        skills=SkillRegistry(tmp_path),
        on_event=tracer,
    ).run("检查非法调用的日志")
    assert calls == original
    assert result.stats.tool_calls[0].arguments["name"] == "coding"
    assert all(record.status == "failed" for record in result.stats.tool_calls)
    for path in (tracer.text_path, tracer.jsonl_path):
        text = path.read_text()
        assert private_content not in text and private_name not in text
        assert "省略正文" in text


@pytest.mark.parametrize(
    "tool_name, arguments",
    [
        ("read_file", {"reads": [{"path": "src/主程序.py", "start_line": 2, "end_line": 8}]}),
        ("move_file", {"source": "a.py", "destination": "b.py", "create_parents": True}),
        ("git_diff", {"cwd": ".", "mode": "staged", "paths": ["a.py"], "context_lines": 3}),
        ("git_log", {"revision": "HEAD~2", "limit": 5, "offset": 1}),
        ("git_status", {"include_untracked": False, "paths": []}),
        ("run_command", {"cwd": "src", "timeout_seconds": 30, "check_id": "unit-tests"}),
        (
            "find_references",
            {
                "path": "a.py",
                "line": 3,
                "column": 4,
                "include_declaration": True,
                "start_index": 0,
                "limit": 20,
            },
        ),
        (
            "read_tool_result",
            {"result_ref": "result:abc", "field": "stderr", "offset": 10, "limit": 50},
        ),
        ("get_execution_environment", {"sections": ["python", "tools"]}),
    ],
)
def test_operation_metadata_survives_trace_serialization(tracer, tool_name, arguments):
    from agent.Tracing import RunTrace

    original = deepcopy(arguments)
    trace = RunTrace(1, tracer)
    token = trace.start_tool(1, ToolCall("metadata", tool_name, arguments))
    trace.finish_tool(token)
    assert arguments == original
    events = [json.loads(line) for line in tracer.jsonl_path.read_text().splitlines()]
    calls = [event["tool_call"] for event in events if event["event"].startswith("tool_")]
    assert len(calls) == 2
    assert all(call["arguments"] == original for call in calls)
    assert json.dumps(original, ensure_ascii=False) in tracer.text_path.read_text()


def test_metadata_summary_is_bounded_and_does_not_reveal_nested_bodies():
    from agent.Tracing import summarize_arguments

    secret = "private code, command arguments or extra fields"
    cases = [
        ("load_tool_group", {"group": "coding", "name": secret, "content": secret}),
        (
            "read_file",
            {"reads": [{"path": "a.py", "content": secret}, secret, {"path": {"nested": secret}}]},
        ),
        ("git_diff", {"paths": [{"content": secret}]}),
        ("run_command", {"command": [secret], "cwd": {"nested": secret}}),
        ("run_python", {"code": secret}),
        ("run_shell", {"script": secret}),
        ("edit_file", {"edits": [{"old_text": secret, "new_text": secret}]}),
        ("apply_patch", {"patch": secret}),
        ("unknown_tool", {"group": secret, "reads": [{"path": secret}], "cwd": secret}),
    ]
    original = deepcopy(cases)
    for tool_name, arguments in cases:
        summary = summarize_arguments(arguments, tool_name=tool_name)
        assert secret not in json.dumps(summary)
    assert cases == original
    for tool_name, field, value in [
        ("git_diff", "paths", "a" * 400),
        ("read_file", "reads", {"path": "a" * 400}),
    ]:
        summary = summarize_arguments({field: [value] * 40}, tool_name=tool_name)
        assert len(summary[field]) == 33  # 32 entries and an explicit omission marker.
        assert summary[field][-1] == "<省略 8 项>"
        assert "截断" in json.dumps(summary, ensure_ascii=False)
    assert summarize_arguments({"edits": [secret] * 4}) == {"edits": "<省略，4 项>"}
