"""Queue durability, scheduling and presentation contracts, without network access."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from session_helpers import Model

from agent import AgentRuntime
from agent.task_queue import TaskQueue, validate_queue
from cli.task_controller import TaskController
from cli.task_execution import TaskOutcome
from cli.terminal.application import ConversationUI
from host_support.cancellation import RunCancelled
from llm import Message


def controller(conversation):
    return TaskController(conversation.runtime, conversation=conversation, write=lambda *_: None)


def saved(conversation):
    return json.loads((conversation.store.directory / f"{conversation.store.id}.json").read_text())


def finish_next(control):
    ticket = control.start_next()
    return control.finish(ticket, control.execute(ticket))


def test_fifo_latest_history_and_durable_results(open_conversation):
    conversation = open_conversation()
    control = controller(conversation)
    control.enqueue("first")
    control.enqueue("second")
    assert not conversation.history and conversation.pending_task is None
    assert len(saved(conversation)["task_queue"]["tasks"]) == 2
    assert finish_next(control) == "completed"
    first_context = control.cancellation
    assert [
        m.content for m in conversation.runtime.llm.requests[0].messages if m.role == "user"
    ] == ["first"]
    first_history = conversation.history
    assert finish_next(control) == "completed"
    assert conversation.runtime.llm.requests[1].messages[:-1] == first_history
    assert first_context is not control.cancellation
    record = saved(conversation)
    assert record["pending_task"] is None
    assert all(t["state"] == "completed" for t in record["task_queue"]["tasks"])
    assert "SYNTHETIC_KEY" not in json.dumps(record["task_queue"])
    assert control.start_next() is None


def test_edit_move_remove_retry_and_configuration_guard(open_conversation):
    control = controller(open_conversation())
    for text in ["one", "two", "three"]:
        control.enqueue(text)
    control.command("/queue pause")
    control.command("/queue edit 2 modified")
    control.command("/queue move 3 1")
    control.command("/queue remove 1")
    assert [t["text"] for t in control.queue.pending] == ["three", "modified"]
    with pytest.raises(ValueError, match="待执行"):
        control.require_idle_configuration()
    control.command("/queue retry 1")
    assert control.queue.pending[-1]["retry_of"] == control.queue.get(1)["id"]
    assert control.start_next() is None
    control.command("/queue clear")
    control.require_idle_configuration()
    assert not control.queue.pending
    with pytest.raises(ValueError):
        control.command("/queue add /apply")


@pytest.mark.parametrize("point", ["enqueue", "start", "finish"])
def test_disk_failure_stops_dispatch_without_reexecuting(open_conversation, monkeypatch, point):
    conversation = open_conversation()
    control = controller(conversation)
    if point != "enqueue":
        control.enqueue("first")
        control.enqueue("second")
    ticket = control.start_next() if point == "finish" else None
    outcome = control.execute(ticket) if ticket else None
    before = saved(conversation)
    original = conversation.store.save

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(conversation.store, "save", fail)
    with pytest.raises(OSError):
        if point == "enqueue":
            control.enqueue("first", submission_id="a" * 32)
        elif point == "start":
            control.start_next()
        else:
            control.finish(ticket, outcome)
    assert control.queue.paused and control.start_next() is None
    assert saved(conversation) == before
    assert len(conversation.runtime.llm.requests) == (point == "finish")
    monkeypatch.setattr(conversation.store, "save", original)
    if point == "enqueue":
        control.enqueue("first", submission_id="a" * 32)
        assert len(control.queue.pending) == 1
    control.resume()
    assert finish_next(control) == "completed"
    requests = conversation.runtime.llm.requests
    assert [r.messages[-1].content for r in requests] == (
        ["first", "second"] if point == "finish" else ["first"]
    )
    if point == "start":
        assert conversation.transcript.render("collapsed")[0].count("你> first") == 1


@pytest.mark.parametrize("kind", ["cancelled", "error", "limit", "writeback", "cleanup"])
def test_non_success_pauses_and_keeps_waiting_tasks(open_conversation, kind):
    conversation = open_conversation()
    control = controller(conversation)
    control.enqueue("first")
    control.enqueue("second")
    ticket = control.start_next()
    if kind == "cancelled":
        control.stop()
        outcome = TaskOutcome("cancelled", RunCancelled(control.cancellation))
    elif kind == "error":
        outcome = TaskOutcome("error", "model failed")
    else:
        outcome = control.execute(ticket)
        if kind == "limit":
            from dataclasses import replace

            outcome.value = replace(outcome.value, status="stopped", resumable=True)
        if kind == "writeback":
            outcome.writeback_ok = False
        if kind == "cleanup":
            outcome.cleanup_status = "unknown"
    assert control.finish(ticket, outcome) != "completed"
    assert control.queue.paused
    assert control.start_next() is None
    assert [t["text"] for t in control.queue.pending] == ["second"]
    if kind == "cleanup":
        with pytest.raises(ValueError, match="进程清理"):
            control.resume()
    else:
        control.resume()
        assert finish_next(control) == "completed"


@pytest.mark.parametrize("running", [False, True])
def test_restore_pauses_and_never_replays_uncertain_work(open_conversation, running):
    conversation = open_conversation()
    control = controller(conversation)
    control.enqueue("first")
    control.enqueue("second")
    if running:
        control.start_next()
    conversation.store.close()
    restored = open_conversation()
    after = controller(restored)
    assert after.queue.paused and after.start_next() is None
    assert not restored.runtime.llm.requests
    assert after.queue.get(1)["state"] == ("interrupted" if running else "queued")
    after.resume()
    finish_next(after)
    assert restored.runtime.llm.requests[0].messages[-1].content == (
        "second" if running else "first"
    )


def test_completed_result_is_not_replayed_when_restarted(open_conversation):
    conversation = open_conversation()
    control = controller(conversation)
    control.enqueue("first")
    finish_next(control)
    conversation.store.close()
    restored = controller(open_conversation())
    assert restored.start_next() is None
    assert restored.queue.get(1)["state"] == "completed"


@pytest.mark.parametrize("mutation", ["duplicate", "two_running", "bad_state", "bad_attempt"])
def test_invalid_queue_records_rejected(mutation):
    queue = TaskQueue()
    queue.add("one")
    queue.add("two")
    value = queue.snapshot()
    if mutation == "duplicate":
        value["tasks"][1] = deepcopy(value["tasks"][0])
    elif mutation == "two_running":
        for task in value["tasks"]:
            task.update(state="running", attempts=[{"id": "a", "started_at": "now", "config": {}}])
    elif mutation == "bad_state":
        value["tasks"][0]["state"] = "broken"
    else:
        value["tasks"][0]["attempts"] = [{}]
    with pytest.raises(ValueError, match="任务队列"):
        validate_queue(value)


def test_queue_update_and_compaction_share_snapshot_lock(open_conversation):
    conversation = open_conversation()
    control = controller(conversation)
    control.enqueue("first")
    control.start_next()
    # This is the same commit entrypoint used by automatic compaction in the worker.
    history = (Message("user", "compacted context"), Message("assistant", "summary"))
    state = {
        "snapshot": "a" * 32,
        "pins": [],
        "prefix": [m.to_dict() for m in history],
        "before": 100,
        "after": 20,
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        commit = executor.submit(conversation.commit_compaction, history, state)
        control.enqueue("second")
        commit.result(timeout=5)
    record = saved(conversation)
    assert record["history"][0]["content"] == "compacted context"
    assert [t["text"] for t in record["task_queue"]["tasks"]] == ["first", "second"]


def test_tui_accepts_tasks_during_run_and_honors_pause():
    started, release = threading.Event(), threading.Event()

    class BlockingModel(Model):
        def generate(self, request):
            if not self.requests:
                started.set()
                assert release.wait(5)
            return super().generate(request)

    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 5)

    async def run():
        model = BlockingModel()
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                AgentRuntime(model, []), terminal_input=pipe, terminal_output=DummyOutput()
            )
            app = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("first\r")
                await until(started.is_set)
                pipe.send_text("second\rthird\r/queue pause\r")
                await until(
                    lambda: len(ui.controller.queue.pending) == 2 and ui.controller.queue.paused
                )
                assert ui.busy and not model.requests
                release.set()
                await until(lambda: not ui.busy)
                assert len(model.requests) == 1
                pipe.send_text("/queue move 3 1\r/queue resume\r")
                await until(lambda: len(model.requests) == 3 and not ui.busy)
                assert [r.messages[-1].content for r in model.requests] == [
                    "first",
                    "third",
                    "second",
                ]
                assert [m.content for m in model.requests[0].messages if m.role == "user"] == [
                    "first"
                ]
                pipe.send_text("\x04")
                await asyncio.wait_for(app, 5)
            finally:
                release.set()
                if not app.done():
                    ui.cancelled.set()
                    ui.app.exit()
                    await asyncio.wait_for(app, 5)

    asyncio.run(run())


def test_line_mode_can_prepare_and_reorder_a_batch(monkeypatch):
    from cli.interactive import run_interactive

    model = Model()
    inputs = iter(
        [
            "/queue pause",
            "/queue add first",
            "/queue add second",
            "/queue move 2 1",
            "/queue resume",
            "/exit",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda *_: next(inputs))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    run_interactive(AgentRuntime(model, []))
    assert [r.messages[-1].content for r in model.requests] == ["second", "first"]


def test_single_task_refuses_to_skip_a_saved_queue(open_conversation):
    from types import SimpleNamespace

    from cli.application import run_single_task

    conversation = open_conversation()
    controller(conversation).enqueue("old waiting task")
    session = SimpleNamespace(
        conversation=conversation, runtime=conversation.runtime, status=conversation.status
    )
    with pytest.raises(ValueError, match="已有待执行队列"):
        run_single_task(SimpleNamespace(task="new", sandbox_writeback="manual"), session, None)
    assert not conversation.runtime.llm.requests


def test_legacy_pending_task_migrates_without_execution(open_conversation):
    conversation = open_conversation()
    conversation.start_task("legacy unfinished task")
    record = saved(conversation)
    record.pop("task_queue")
    conversation.store.save(record)
    conversation.store.close()
    restored = open_conversation()
    assert restored.queue.get(1)["state"] == "interrupted"
    assert restored.queue.paused and not restored.queue.pending
    assert not restored.runtime.llm.requests


def test_tui_exit_waits_for_current_and_does_not_start_waiting(open_conversation):
    started, release = threading.Event(), threading.Event()

    class BlockingModel(Model):
        def generate(self, request):
            started.set()
            assert release.wait(5)
            return super().generate(request)

    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 5)

    async def run():
        model = BlockingModel()
        conversation = open_conversation(model=model)
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                conversation.runtime,
                conversation=conversation,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            app = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("first\r")
                await until(started.is_set)
                pipe.send_text("second\r")
                await until(lambda: len(ui.controller.queue.pending) == 1)
                ui.app.exit()
                await until(lambda: ui.controller.closing)
                assert not app.done()
                release.set()
                await asyncio.wait_for(app, 5)
                record = saved(conversation)
                assert record["task_queue"]["tasks"][0]["state"] == "cancelled"
                assert record["task_queue"]["tasks"][1]["state"] == "queued"
                assert record["task_queue"]["paused"]
                assert len(model.requests) == 1
            finally:
                release.set()
                if not app.done():
                    ui.app.exit()
                    await asyncio.wait_for(app, 5)

    asyncio.run(run())
