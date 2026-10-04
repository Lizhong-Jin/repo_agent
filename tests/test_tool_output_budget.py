"""Model output limits never truncate durable receipts or fabricate success."""

import json
from threading import Barrier

import pytest
from test_parallel_runtime import ParallelTool, model
from test_runtime import ScriptedLLM, reply

from agent import AgentRuntime
from agent.execution_ledger import ExecutionLedger
from agent.session import SessionStore
from agent.tool_output import ToolOutputBudget
from llm import ToolCall
from tools import ReadFileTool, ToolResult


def test_read_projection_preserves_complete_lines_hash_and_continuation(tmp_path):
    (tmp_path / "a").write_text(('quotes "\\\t\r and 中文\n') * 500)
    call = ToolCall("a", "read_file", {"reads": [{"path": "a"}]})
    original = ReadFileTool(tmp_path).execute(call.arguments).to_message(call)
    projected = ToolOutputBudget([call], 2048).project(call, original)
    assert len(projected.content) <= 2048
    page = json.loads(projected.content)
    data = page["data"]["results"][0]["data"]
    full = json.loads(original.content)["data"]["results"][0]["data"]
    assert data["content"] and full["content"].startswith(data["content"])
    assert data["sha256"] == full["sha256"]
    assert data["next_start_line"] == data["end_line"] + 1
    assert data["end_line"] == len(data["content"].split("\n"))
    assert data["truncated"] and page["output_truncated"]
    followup = ReadFileTool(tmp_path).execute(
        {
            "reads": [
                {"path": "a", "start_line": data["next_start_line"]},
            ]
        }
    )
    assert followup.success
    assert json.loads(original.content)["data"]["results"][0]["data"] == full


def test_read_line_too_long_has_no_fragment_and_resumable_offset(tmp_path):
    (tmp_path / "a").write_text("x" * 5000)
    call = ToolCall("a", "read_file", {"reads": [{"path": "a"}]})
    original = ReadFileTool(tmp_path).execute(call.arguments).to_message(call)
    page = json.loads(ToolOutputBudget([call], 1024).project(call, original).content)
    data = page["data"]["results"][0]["data"]
    assert data["content"] == "" and data["next_start_line"] == 1
    assert data["end_line"] == 0 and data["truncated"]


@pytest.mark.parametrize("success", [True, False])
def test_generic_payload_omission_preserves_status_and_cleanup(success):
    call = ToolCall("a", "run_command", {})
    result = ToolResult(
        success,
        {"stdout": "x" * 5000, "exit_code": 7, "cleanup_status": "unknown"},
        error_code="\x00" * 1000,
        error="details" * 1000,
    )
    original = result.to_message(call)
    bounded = ToolOutputBudget([call], 1024).project(call, original)
    page = json.loads(bounded.content)
    assert len(bounded.content) <= 1024 and bounded.is_error == (not success)
    assert page["success"] == success and page["output_truncated"]
    assert page["data"]["cleanup_status"] == "unknown"
    assert page["data"]["exit_code"] == 7
    assert len(original.content) > 5000


def test_parallel_round_is_bounded_and_full_receipts_are_durable(tmp_path):
    both = Barrier(2)

    def execute(value):
        both.wait(3)
        return ToolResult(True, {"value": value, "body": "large output\n" * 1000})

    runtime = AgentRuntime(
        model("slow", "fast"), [ParallelTool(execute)], max_tool_output_chars=2048
    )
    store = SessionStore(tmp_path).open()
    try:
        runtime.execution_ledger = ExecutionLedger(store)
        result = runtime.run("read")
        messages = [m for m in result.history if m.role == "tool"]
        assert [m.tool_call_id for m in messages] == ["slow", "fast"]
        assert sum(len(m.content) for m in messages) <= 2048
        assert all(json.loads(m.content)["output_truncated"] for m in messages)
        evidence = runtime.execution_ledger.evidence()
        assert all(len(row["observation"]["content"]) > 10000 for row in evidence)
        assert all(row["state"] == "returned" for row in evidence)
    finally:
        store.close()


def test_projection_is_independent_of_completion_order():
    calls = [ToolCall(str(i), "read", {}) for i in range(3)]
    observations = [ToolResult(True, {"body": "z" * 10000}).to_message(c) for c in calls]
    budget = ToolOutputBudget(calls, 4097)
    first = {c.id: budget.project(c, o) for c, o in zip(calls, observations, strict=True)}
    second = {
        c.id: budget.project(c, o) for c, o in reversed(list(zip(calls, observations, strict=True)))
    }
    assert first == second
    assert sum(len(m.content) for m in first.values()) <= 4097


def test_too_many_calls_stops_before_execution_and_keeps_valid_history():
    calls = [ToolCall(str(i), "parallel_read", {"value": "a"}) for i in range(3)]
    llm = ScriptedLLM([reply(calls=calls)])
    runtime = AgentRuntime(
        llm, [ParallelTool(lambda _: pytest.fail("executed"))], max_tool_output_chars=2048
    )
    result = runtime.run("read")
    assert result.status == "stopped" and result.resumable
    assert all(not m.tool_calls and m.role != "tool" for m in result.history)


@pytest.mark.parametrize("value", [True, 0, 1023, 1.5])
def test_invalid_round_limits(value):
    with pytest.raises(ValueError, match="max_tool_output_chars"):
        AgentRuntime(ScriptedLLM([]), max_tool_output_chars=value)
