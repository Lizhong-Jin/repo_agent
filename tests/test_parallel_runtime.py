"""Parallel calls retain ordered history, accurate events and durable receipts."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, get_ident

import pytest
from test_runtime import ScriptedLLM, reply

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from agent.Tracing import format_event
from cli.session_status import SessionStatus
from cli.task_controller import TaskController
from host_support.cancellation import CancellationContext, current_cancellation
from host_support.execution_receipt import PersistenceError
from llm import LLMConfig, ToolCall, ToolDefinition
from tools import ExecutionKind, ToolResult
from tools.scheduling import INDEPENDENT


class ParallelTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    scheduling_policy = INDEPENDENT
    definition = ToolDefinition("parallel_read", "test read")

    def __init__(self, invoke):
        self.invoke = invoke

    def execute(self, arguments):
        return self.invoke(arguments["value"])


def model(*values):
    calls = [ToolCall(value, "parallel_read", {"value": value}) for value in values]
    result = ScriptedLLM([reply(calls=calls), reply("done")])
    result.config = LLMConfig("deepseek", "m", api_key="test")
    return result


def test_history_order_and_out_of_order_trace_completion():
    second_finished = Event()
    callback_threads = set()
    events = []

    def invoke(value):
        if value == "first":
            assert second_finished.wait(3)
        return ToolResult(True, {"value": value})

    def event(name, stats):
        callback_threads.add(get_ident())
        if name == "tool_end":
            events.append((stats.current_tool.call_id, format_event(name, stats)))
            if stats.current_tool.call_id == "second":
                second_finished.set()

    runtime = AgentRuntime(model("first", "second"), [ParallelTool(invoke)], on_event=event)
    cancellation = CancellationContext()
    result = runtime.run("read", cancellation=cancellation)
    assert [m.tool_call_id for m in result.history if m.role == "tool"] == ["first", "second"]
    assert [call for call, _ in events] == ["second", "first"]
    assert all(f'id="{call}"' in text for call, text in events)
    assert [call.call_id for call in result.stats.tool_calls] == ["first", "second"]
    assert all(call.status == "success" for call in result.stats.tool_calls)
    assert callback_threads == {get_ident()}
    assert {item["call_id"] for item in cancellation.report()["tools"]} == {"first", "second"}


@pytest.mark.parametrize("fail_receipt", [False, True])
def test_receipts_commit_before_batch_join_and_failures_stop_queue(
    tmp_path, monkeypatch, fail_receipt
):
    started = Barrier(2)
    release = Event()
    receipt_done = Event()
    slow_cleaned = Event()

    def invoke(value):
        started.wait(3)
        if value == "slow":
            if fail_receipt:
                assert current_cancellation().event.wait(3)
            assert release.wait(3)
            slow_cleaned.set()
        return ToolResult(True, {"value": value})

    store = SessionStore(tmp_path).open()
    runtime = AgentRuntime(model("slow", "fast"), [ParallelTool(invoke)])
    conversation = SavedConversation(store, runtime, runtime.llm.config, SessionStatus(tmp_path))
    control = TaskController(runtime, conversation=conversation, write=lambda *_: None)
    original_finish = conversation.ledger.finish

    def finish(run, call, *args, **kwargs):
        if call == "fast" and fail_receipt:
            receipt_done.set()
            raise PersistenceError("disk full")
        original_finish(run, call, *args, **kwargs)
        if call == "fast":
            receipt_done.set()

    monkeypatch.setattr(conversation.ledger, "finish", finish)
    try:
        control.enqueue("read")
        control.enqueue("queued next task")
        ticket = control.start_next()
        cursor = conversation.ledger_cursor
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(control.execute, ticket)
            try:
                assert receipt_done.wait(3)
                assert not future.done() and control.start_next() is None
                assert conversation.ledger_cursor == cursor
                rows = {r["call_id"]: r for r in conversation.ledger.evidence()}
                assert rows["slow"]["state"] == "started"
                assert rows["fast"]["state"] == ("started" if fail_receipt else "returned")
                if not fail_receipt:
                    assert json.loads(rows["fast"]["observation"]["content"])["data"] == {
                        "value": "fast"
                    }
            finally:
                release.set()
            outcome = future.result(timeout=3)
        assert slow_cleaned.is_set()
        assert control.finish(ticket, outcome) == ("failed" if fail_receipt else "completed")
        rows = {r["call_id"]: r for r in conversation.ledger.evidence()}
        assert rows["slow"]["state"] == "returned"
        if fail_receipt:
            assert "PersistenceError" in outcome.value
            assert control.queue.paused and control.start_next() is None
            assert len(runtime.llm.requests) == 1
        else:
            assert len(runtime.llm.requests) == 2
    finally:
        store.close()


def test_runtime_rejects_overlapping_runs():
    entered, release = Event(), Event()

    def invoke(_):
        entered.set()
        assert release.wait(3)
        return ToolResult(True)

    runtime = AgentRuntime(model("one"), [ParallelTool(invoke)])
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(runtime.run, "first")
        try:
            assert entered.wait(3)
            with pytest.raises(RuntimeError, match="active run"):
                runtime.run("second")
        finally:
            release.set()
        assert first.result(timeout=3).status == "completed"
