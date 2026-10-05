"""Saved-result retrieval, isolation, durability and lossless output-budget pagination."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_runtime import ScriptedLLM, reply

from agent import AgentRuntime
from agent.execution_ledger import ExecutionLedger
from agent.tool_output import ToolOutputBudget
from agent.tool_results import ReadToolResultTool
from host_support.cancellation import RunCancelled, cancellation_scope
from host_support.execution_receipt import PersistenceError
from llm import Message, ToolCall, ToolDefinition
from tools import ExecutionKind, ToolEffects, ToolResult
from tools.scheduling import INDEPENDENT


@pytest.fixture
def ledger(tmp_path):
    return ExecutionLedger(SimpleNamespace(directory=tmp_path, id=uuid4().hex, data={}))


def save(ledger, data=None, *, success=True, certain=True):
    call = ToolCall("original", "run_command", {})
    run = uuid4().hex
    ledger.begin(run, {})
    ledger.prepare(run, Message("assistant", tool_calls=[call]))
    ledger.start(run, call.id)
    observation = ToolResult(success, data or {}, error_code="FAILED", error="details").to_message(
        call
    )
    ref = ledger.finish(run, call.id, observation, {}, certain=certain)
    return ref, observation


def read(ledger, ref, **kwargs):
    return ReadToolResultTool(ledger.read_result).execute({"result_ref": ref, **kwargs})


@pytest.mark.parametrize("field", ["result", "stdout", "stderr"])
def test_unicode_pages_reconstruct_exact_saved_result(ledger, field):
    data = {"stdout": '中文🙂e\u0301\r\n"\\\x00' * 30, "stderr": "warnings\n" * 4}
    ref, original = save(ledger, data)
    expected = original.content if field == "result" else data[field]
    offset = 0
    pages = []
    while True:
        result = read(ledger, ref, field=field, offset=offset, limit=7)
        assert result.success and result.effects == ToolEffects("none")
        page = result.data
        assert page["offset"] == offset and page["total_chars"] == len(expected)
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + len(page["content"])
        offset = page["next_offset"]
    assert "".join(pages) == expected
    assert read(ledger, ref, field=field, offset=len(expected)).data["content"] == ""
    assert (
        read(ledger, ref, field=field, offset=len(expected) + 1).error_code
        == "RESULT_OFFSET_OUT_OF_RANGE"
    )


def test_failure_and_original_capture_loss_are_not_misrepresented(ledger):
    ref, _ = save(
        ledger,
        {"stdout": "partial", "stdout_truncated": True, "output_complete": False},
        success=False,
        certain=False,
    )
    page = read(ledger, ref, field="stdout")
    assert page.success and page.data["source_success"] is False
    assert page.data["source_state"] == "uncertain"
    assert page.data["capture"] == {"stdout_truncated": True, "output_complete": False}
    assert page.data["next_offset"] is None  # end of saved output, not original process output
    assert read(ledger, ref, field="stderr").error_code == "RESULT_FIELD_UNAVAILABLE"
    empty_ref, _ = save(ledger, {"stdout": "", "stderr": 123})
    assert read(ledger, empty_ref, field="stdout").data["content"] == ""
    assert read(ledger, empty_ref, field="stderr").error_code == "RESULT_FIELD_UNAVAILABLE"


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        {},
        {"result_ref": "../../file"},
        {"result_ref": 123},
        {"result_ref": "result_" + "a" * 32 + "_9999999999999999999"},
        {"field": []},
        {"field": "data"},
        {"offset": True},
        {"offset": -1},
        {"offset": 1.5},
        {"offset": 2**63},
        {"limit": True},
        {"limit": 0},
        {"limit": 8001},
        {"session": "other"},
        {"path": "execution.sqlite3"},
    ],
)
def test_invalid_arguments_do_not_access_storage(arguments):
    if isinstance(arguments, dict) and arguments and "result_ref" not in arguments:
        arguments = {"result_ref": "result_" + "a" * 32 + "_1", **arguments}
    tool = ReadToolResultTool(lambda _: pytest.fail("lookup after invalid arguments"))
    assert tool.execute(arguments).error_code == "INVALID_ARGUMENTS"


def test_references_survive_reopen_but_cannot_cross_session_or_project(ledger, tmp_path):
    ref, original = save(ledger, {"stdout": "saved"})
    reopened = ExecutionLedger(ledger.store)
    assert read(reopened, ref).data["content"] == original.content
    current = ledger.store.id
    ledger.store.id = uuid4().hex
    assert read(ledger, ref).error_code == "RESULT_NOT_FOUND"
    ledger.store.id = current
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = ExecutionLedger(SimpleNamespace(directory=other_dir, id=current, data={}))
    other_ref, _ = save(other, {"stdout": "other project"})
    assert other_ref != ref
    assert read(other, ref).error_code == "RESULT_NOT_FOUND"
    assert read(ledger, ref).success
    assert ref in ledger.describe()


def test_unknown_and_unfinished_refs_do_not_create_or_expose_results(ledger):
    assert read(ledger, ledger.result_reference(1)).error_code == "RESULT_NOT_FOUND"
    assert not ledger.path.exists()
    run = uuid4().hex
    call = ToolCall("pending", "run_command", {})
    ledger.begin(run, {})
    ledger.prepare(run, Message("assistant", tool_calls=[call]))
    ledger.start(run, call.id)
    assert (
        read(ledger, ledger.result_reference(ledger.watermark())).error_code == "RESULT_NOT_FOUND"
    )


def test_corrupt_or_missing_required_ledger_stops_execution(ledger):
    ref, _ = save(ledger)
    with ledger.connect(write=True) as db:
        db.execute("UPDATE calls SET observation='{}'")
    with pytest.raises(PersistenceError):
        read(ledger, ref)
    ledger.path.unlink()
    with pytest.raises(PersistenceError):
        read(ledger, ref)


def test_read_size_guard_and_cancellation(ledger, monkeypatch):
    ref, _ = save(ledger, {"stdout": "abc"})
    monkeypatch.setattr("agent.execution_ledger.MAX_RESULT_CHARS", 1)
    assert read(ledger, ref).error_code == "RESULT_TOO_LARGE"
    with cancellation_scope() as context:
        context.cancel()
        with pytest.raises(RunCancelled):
            read(ledger, ref)


def test_parallel_readers_return_same_snapshot_without_writes(ledger):
    ref, original = save(ledger, {"stdout": "content"})
    watermark = ledger.watermark()
    with ThreadPoolExecutor(4) as pool:
        pages = list(pool.map(lambda _: read(ledger, ref), range(16)))
    assert all(page.data["content"] == original.content for page in pages)
    assert ledger.watermark() == watermark
    assert ReadToolResultTool.scheduling_policy is INDEPENDENT


class LargeTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    definition = ToolDefinition("produce", "Produce saved output")

    def __init__(self):
        self.calls = 0
        self.output = '中🙂\x00\\"\n' * 1000

    def execute(self, arguments):
        self.calls += 1
        return ToolResult(
            True, {"stdout": self.output, "stderr": "warning", "stdout_truncated": True}
        )


def test_runtime_publishes_reference_and_reads_after_budget_omission(ledger):
    producer = LargeTool()
    chunks = []

    class Model:
        def generate(self, request):
            assert "read_tool_result" in {d.name for d in request.tools}
            messages = [m for m in request.messages if m.role == "tool"]
            if not messages:
                return reply(calls=[ToolCall("produce-1", "produce", {})])
            latest = json.loads(messages[-1].content)
            assert len(messages[-1].content) <= 1024
            if len(messages) == 1:
                assert latest["output_truncated"] and not latest["data"].get("stdout")
                self.ref = latest["result_ref"]
                offset = 0
            else:
                page = latest["data"]
                assert page["result_ref"] == self.ref
                assert page["offset"] == sum(map(len, chunks))
                chunks.append(page["content"])
                assert page["capture"]["stdout_truncated"]
                offset = page["next_offset"]
                if offset is None:
                    return reply("complete")
                assert page["content"]  # every projected page makes progress
            return reply(
                calls=[
                    ToolCall(
                        f"page-{len(messages)}",
                        "read_tool_result",
                        {
                            "result_ref": self.ref,
                            "field": "stdout",
                            "offset": offset,
                            "limit": 8000,
                        },
                    )
                ]
            )

    runtime = AgentRuntime(Model(), [producer], max_steps=0, max_tool_output_chars=1024)
    assert "read_tool_result" not in runtime._tools
    runtime.execution_ledger = ledger
    runtime.execution_ledger = ledger  # idempotent attachment
    result = runtime.run("get output")
    assert result.status == "completed" and producer.calls == 1
    assert "".join(chunks) == producer.output
    original = next(row for row in ledger.evidence(limit=1000) if row["name"] == "produce")
    assert json.loads(original["observation"]["content"])["data"]["stdout"] == producer.output


def test_projected_json_pages_keep_source_reference_and_continuation(ledger):
    ref, original = save(ledger, {"stdout": "\x00" * 8000})
    call = ToolCall("page", "read_tool_result", {"result_ref": ref})
    page = read(ledger, ref, limit=8000).to_message(call)
    projected = ToolOutputBudget([call], 1024).project(call, page)
    body = json.loads(projected.content)["data"]
    assert len(projected.content) <= 1024
    assert body["content"] and original.content.startswith(body["content"])
    assert body["next_offset"] == len(body["content"])
    assert body["result_ref"] == ref


def test_no_ledger_does_not_advertise_a_reader_or_unrecoverable_ref():
    runtime = AgentRuntime(
        ScriptedLLM([reply(calls=[ToolCall("1", "produce", {})]), reply("done")]),
        [LargeTool()],
        max_tool_output_chars=1024,
    )
    result = runtime.run("produce")
    assert "read_tool_result" not in {d.name for d in runtime._definitions}
    assert "result_ref" not in json.loads(
        next(m.content for m in result.history if m.role == "tool")
    )


def test_saved_conversation_restore_clear_and_new_session_scope(tmp_path):
    from agent.conversation import SavedConversation
    from agent.session import SessionStore
    from cli.session_status import SessionStatus
    from llm import LLMConfig

    config = LLMConfig("deepseek", "m", api_key="test")
    store = SessionStore(tmp_path).open()
    try:
        runtime = AgentRuntime(
            ScriptedLLM([reply(calls=[ToolCall("write", "produce", {})]), reply("done")]),
            [LargeTool()],
            max_tool_output_chars=1024,
        )
        conversation = SavedConversation(store, runtime, config, SessionStatus(tmp_path))
        conversation.start_task("produce")
        result = runtime.run("produce")
        conversation.finish_task(result)
        ref = json.loads(next(m.content for m in result.history if m.role == "tool"))["result_ref"]
    finally:
        store.close()
    store = SessionStore(tmp_path).open()
    try:
        runtime = AgentRuntime(ScriptedLLM([]))
        conversation = SavedConversation(store, runtime, config, SessionStatus(tmp_path))
        assert "read_tool_result" in {d.name for d in runtime._definitions}

        def retrieve():
            return runtime.dispatcher.execute(
                "read_tool_result", {"result_ref": ref, "field": "stderr"}
            )

        assert retrieve().data["content"] == "warning"
        conversation.clear()
        assert retrieve().success  # clear context does not delete execution evidence
        conversation.new_session()
        assert retrieve().error_code == "RESULT_NOT_FOUND"
    finally:
        store.close()


def test_ledger_rebinding_is_rejected_during_run(ledger):
    runtime = AgentRuntime(ScriptedLLM([]))
    with runtime._run_lock, pytest.raises(RuntimeError, match="active run"):
        runtime.execution_ledger = ledger
    assert runtime.execution_ledger is None and "read_tool_result" not in runtime._tools


def test_parallel_runtime_results_have_distinct_refs_for_original_calls(ledger):
    from threading import Barrier

    from test_parallel_runtime import ParallelTool, model

    barrier = Barrier(2)

    def invoke(value):
        barrier.wait(3)
        return ToolResult(True, {"stdout": value * 2000})

    runtime = AgentRuntime(
        model("slow", "fast"), [ParallelTool(invoke)], max_tool_output_chars=2048
    )
    runtime.execution_ledger = ledger
    result = runtime.run("parallel")
    outputs = [json.loads(m.content) for m in result.history if m.role == "tool"]
    assert len({output["result_ref"] for output in outputs}) == 2
    for output, value in zip(outputs, ["slow", "fast"], strict=True):
        page = read(ledger, output["result_ref"], field="stdout", limit=8000)
        assert page.data["content"] == value * 2000


def test_reader_can_be_moved_to_a_group_without_stale_loader_catalog(ledger):
    from tools import ToolGroup

    runtime = AgentRuntime(
        ScriptedLLM([]),
        tool_groups=[ToolGroup("evidence", "Saved evidence", ("read_tool_result",))],
    )
    runtime.execution_ledger = ledger
    definitions = {d.name: d for d in runtime._definitions}
    assert "read_tool_result" not in definitions
    assert "Available tools: read_tool_result" in definitions["load_tool_group"].description
    assert runtime.tool_groups.load("evidence").success
    assert "read_tool_result" in {d.name for d in runtime._definitions}
