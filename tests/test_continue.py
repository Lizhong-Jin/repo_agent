"""One-command continuation grants a fresh task budget without replaying work."""

import asyncio

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_interactive import Model, inputs, reply
from test_tui import until

from agent import AgentRuntime
from cli.interactive import run_interactive
from cli.task_controller import TaskController
from cli.terminal.application import ConversationUI
from llm import LLMConfig, ToolCall
from tools.filesystem import ReadFileTool


def run_next(control):
    ticket = control.start_next()
    outcome = control.execute(ticket)
    control.finish(ticket, outcome)
    return ticket, outcome


def stopped(open_conversation):
    model = Model(
        [
            reply("part 1", finish="length"),
            reply("part 2", finish="length"),
            reply("part 3", finish="length"),
            reply("part 4", finish="length"),
            reply("finished"),
            reply("later finished"),
        ]
    )
    model.config = LLMConfig("deepseek", "m", api_key="test")
    conversation = open_conversation(model=model)
    conversation.runtime.max_steps = 2
    control = TaskController(conversation.runtime, conversation=conversation, write=lambda *_: None)
    control.enqueue("original request")
    run_next(control)
    assert control.queue.paused and len(model.requests) == 2
    return conversation, control, model


def test_continue_prioritizes_original_and_grants_same_budget_each_time(open_conversation):
    conversation, control, model = stopped(open_conversation)
    control.enqueue("later request")
    history = conversation.history
    first = control.cancellation
    assert "再调用模型 2 轮" in control.command("/continue")
    assert control.queue.pending[0]["continuation_of"] == control.queue.get(1)["id"]
    ticket, outcome = run_next(control)
    assert outcome.value.status == "max_steps" and len(model.requests) == 4
    assert control.cancellation is not first
    assert model.requests[2].messages[:-1] == history
    assert control.runtime.max_steps == 2 and control.queue.paused
    control.command("/continue")
    _, outcome = run_next(control)
    assert outcome.value.status == "completed"
    assert control.queue.get(4)["continuation_of"] == ticket["id"]
    assert run_next(control)[0]["text"] == "later request"
    assert len(model.requests) == 6


def test_continue_survives_restart_and_failed_save_without_duplicate(
    open_conversation, monkeypatch
):
    conversation, _, _ = stopped(open_conversation)
    conversation.store.close()
    restored = open_conversation()
    control = TaskController(restored.runtime, conversation=restored)
    original = restored.store.save

    def fail(_):
        raise OSError("disk full")

    monkeypatch.setattr(restored.store, "save", fail)
    with pytest.raises(OSError):
        control.command("/continue")
    assert len(control.queue.pending) == 1
    assert control.start_next() is None
    monkeypatch.setattr(restored.store, "save", original)
    control.command("/continue")
    assert len(control.queue.pending) == 1
    assert run_next(control)[1].value.status == "completed"
    with pytest.raises(ValueError, match="没有可继续"):
        control.command("/continue")


@pytest.mark.parametrize("reason", ["busy", "cleanup", "cleared", "cancelled", "error", "args"])
def test_continue_rejects_unrelated_or_unsafe_states(open_conversation, reason):
    conversation, control, _ = stopped(open_conversation)
    if reason == "busy":
        control.active = control.queue.get(1)
    elif reason == "cleanup":
        control.cleanup_blocked = True
    elif reason == "cleared":
        conversation.clear()
    elif reason == "cancelled":
        control.queue.get(1).update(state="cancelled")
    elif reason == "error":
        control.queue.get(1)["result"]["run_status"] = "stopped"
    before = control.queue.snapshot()
    with pytest.raises(ValueError):
        control.command("/continue extra" if reason == "args" else "/continue")
    assert control.queue.snapshot() == before


def test_continue_accepts_max_steps_with_automatic_writeback_deferred(open_conversation):
    _, control, _ = stopped(open_conversation)
    # on-success deliberately declines writeback for max_steps, keeping the work copy.
    control.queue.get(1)["state"] = "failed"
    control.queue.get(1)["result"]["writeback_ok"] = False
    control.command("/continue")
    assert control.start_next()["continuation_of"] == control.queue.get(1)["id"]


def test_line_continue_preserves_tool_results_and_automatically_runs(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("tool result marker")
    model = Model(
        [
            reply(calls=[ToolCall("read-once", "read_file", {"reads": [{"path": "a"}]})]),
            reply("finished"),
        ]
    )
    inputs(monkeypatch, ["read and summarize", "/continue", "/exit"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    run_interactive(AgentRuntime(model, [ReadFileTool(tmp_path)], max_steps=1))
    assert len(model.requests) == 2
    assert any(
        m.role == "tool" and "tool result marker" in m.content for m in model.requests[1].messages
    )
    assert sum(bool(m.tool_calls) for m in model.requests[1].messages) == 1


def test_tui_continue_starts_without_queue_resume(tmp_path):
    (tmp_path / "a").write_text("content")
    model = Model(
        [
            reply(calls=[ToolCall("read-once", "read_file", {"reads": [{"path": "a"}]})]),
            reply("continued successfully"),
        ]
    )

    async def run():
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                AgentRuntime(model, [ReadFileTool(tmp_path)], max_steps=1),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            app = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("read and summarize\r")
                await until(lambda: len(model.requests) == 1 and not ui.busy)
                assert "/continue" in ui.transcript
                assert "/continue" in ui.phase
                pipe.send_text("/continue\r")
                await until(lambda: len(model.requests) == 2 and not ui.busy)
                assert "continued successfully" in ui.transcript
                assert not ui.controller.queue.paused
                assert ui.controller.queue.get(2)["state"] == "completed"
                pipe.send_text("/exit\r")
                await asyncio.wait_for(app, 5)
            finally:
                if not app.done():
                    ui.app.exit()
                    await asyncio.wait_for(app, 5)

    asyncio.run(run())
