"""Durable evidence across real process loss and required-write failures."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from cli.session_status import SessionStatus
from cli.task_controller import TaskController
from host_support.cancellation import current_cancellation
from host_support.execution_receipt import PersistenceError
from llm import LLMConfig, LLMResponse, Message, ToolCall, ToolDefinition
from tools import ExecutionKind, ToolResult


class Model:
    config = LLMConfig("deepseek", "m", api_key="test")

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        calls = [
            ToolCall("write-1", "write_file", {"path": "artifact"}),
            ToolCall("write-2", "write_file", {"path": "later"}),
        ]
        return LLMResponse(
            "deepseek", "m", Message("assistant", tool_calls=calls), finish_reason="tool_calls"
        )


class Write:
    execution_kind = ExecutionKind.HOST_CONTROL
    definition = ToolDefinition("write_file", "test trusted writer")

    def __init__(self, root, *, cancel=False, fail=False):
        self.root, self.cancel, self.fail = root, cancel, fail

    def execute(self, arguments):
        path = self.root / arguments["path"]
        path.write_text("effect")
        if self.cancel:
            current_cancellation().cancel()
        if self.fail:
            raise RuntimeError("after effect")
        return ToolResult(True, {"path": path.name, "bytes_written": 6})


def create(tmp_path, tool):
    store = SessionStore(tmp_path).open()
    runtime = AgentRuntime(Model(), [tool], max_steps=1)
    conversation = SavedConversation(store, runtime, runtime.llm.config, SessionStatus(tmp_path))
    return (
        store,
        conversation,
        TaskController(runtime, conversation=conversation, write=lambda *_: None),
    )


def test_cancellation_commits_return_before_checkpoint(tmp_path):
    store, conversation, control = create(tmp_path, Write(tmp_path, cancel=True))
    try:
        control.enqueue("write")
        control.enqueue("waiting")
        ticket = control.start_next()
        outcome = control.execute(ticket)
        assert outcome.kind == "cancelled"
        assert control.finish(ticket, outcome) == "cancelled"
        rows = conversation.ledger.evidence()
        states = {r["call_id"]: r["state"] for r in rows}
        assert states == {"write-1": "returned", "write-2": "intended"}
        assert not (tmp_path / "later").exists()
        assert control.queue.paused and control.start_next() is None
        run = next(row for row in rows if row["call_id"] == "write-1")
        assert run["run"] == ticket["attempts"][0]["id"]
        assert run["identity"]["queue_task_id"] == ticket["id"]
        assert json.loads(run["observation"]["content"])["data"]["bytes_written"] == 6
    finally:
        store.close()


@pytest.mark.parametrize("stage", ["begin", "prepare", "start", "finish"])
def test_required_ledger_failure_stops_run_and_queue(tmp_path, monkeypatch, stage):
    store, conversation, control = create(tmp_path, Write(tmp_path))
    try:
        control.enqueue("write")
        control.enqueue("waiting")
        ticket = control.start_next()

        def fail(*args, **kwargs):
            raise PersistenceError("injected disk full")

        monkeypatch.setattr(conversation.ledger, stage, fail)
        outcome = control.execute(ticket)
        assert outcome.kind == "error" and "PersistenceError" in outcome.value
        assert control.finish(ticket, outcome) == "failed"
        assert control.start_next() is None
        assert (tmp_path / "artifact").exists() == (stage == "finish")
        assert not (tmp_path / "later").exists()
        assert len(conversation.runtime.llm.requests) <= 1
    finally:
        store.close()


def test_unexpected_tool_exception_keeps_uncertain_result_and_stops(tmp_path):
    store, conversation, control = create(tmp_path, Write(tmp_path, fail=True))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        outcome = control.execute(ticket)
        assert outcome.kind == "error"
        control.finish(ticket, outcome)
        rows = conversation.ledger.evidence()
        assert next(r for r in rows if r["call_id"] == "write-1")["state"] == "uncertain"
        assert not (tmp_path / "later").exists()
    finally:
        store.close()


CRASH_SCRIPT = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_execution_ledger import create, Write
root = Path(sys.argv[1])
stage = sys.argv[2]
class CrashWrite(Write):
    execution_kind = Write.execution_kind
    def execute(self, arguments):
        result = super().execute(arguments)
        if stage == "effect":
            os._exit(73)
        return result
store, conversation, control = create(root, CrashWrite(root))
ledger = conversation.ledger
original_start, original_finish = ledger.start, ledger.finish
def start(*args):
    if stage == "intent":
        os._exit(73)
    original_start(*args)
    if stage == "start":
        os._exit(73)
def finish(*args, **kwargs):
    original_finish(*args, **kwargs)
    os._exit(73)
ledger.start, ledger.finish = start, finish
control.enqueue("write")
control.enqueue("waiting")
ticket = control.start_next()
control.execute(ticket)
raise AssertionError("expected hard exit")
"""


@pytest.mark.parametrize(
    "stage,state,effect",
    [
        ("intent", "intended", False),
        ("start", "started", False),
        ("effect", "started", True),
        ("result", "returned", True),
    ],
)
def test_process_loss_recovers_evidence_without_replay(tmp_path, stage, state, effect):
    result = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(tmp_path), stage],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 73, result.stderr.decode()
    store, conversation, control = create(tmp_path, Write(tmp_path))
    try:
        assert control.start_next() is None and control.queue.paused
        assert control.queue.get(1)["state"] == "interrupted"
        assert not conversation.runtime.llm.requests
        assert (tmp_path / "artifact").exists() == effect
        assert not (tmp_path / "later").exists()
        rows = conversation.ledger.evidence()
        assert next(r for r in rows if r["call_id"] == "write-1")["state"] == state
        assert "执行恢复证据" in conversation.history[-1].content
        # Partial tool batches are evidence, never fabricated protocol messages.
        assert all(message.role != "tool" for message in conversation.history)
        conversation.checkpoint(strict=True)
    finally:
        store.close()
    store, again, _ = create(tmp_path, Write(tmp_path))
    try:
        assert sum("执行恢复证据" in m.content for m in again.history) == 1
    finally:
        store.close()


@pytest.mark.parametrize("filename", ["snapshot", "index", "latest.json"])
def test_interrupted_snapshot_commit_is_repaired_before_load(
    open_conversation, monkeypatch, filename
):
    first = open_conversation()
    first.history = (Message("user", "new durable content"),)
    original = first.store.catalog._write
    target = f"{first.store.id}.json" if filename == "snapshot" else filename

    def fail(path, data):
        if path.name == target:
            raise OSError("publication interrupted")
        return original(path, data)

    monkeypatch.setattr(first.store.catalog, "_write", fail)
    with pytest.raises(OSError):
        first.checkpoint(strict=True)
    assert (first.store.directory / ".pending-save").exists()
    first.store.close()
    restored = open_conversation()
    assert restored.history[-1].content == "new durable content"
    assert not (restored.store.directory / ".pending-save").exists()
    record = json.loads((restored.store.directory / f"{restored.store.id}.json").read_text())
    index = json.loads((restored.store.directory / "index").read_text())
    assert record["commit_revision"] == index["commit_revision"]
    assert restored.store.catalog.latest_id() == restored.store.id


def test_missing_required_ledger_is_not_silently_recreated(tmp_path):
    store, conversation, control = create(tmp_path, Write(tmp_path, cancel=True))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        control.finish(ticket, control.execute(ticket))
        conversation.ledger.path.unlink()
        with pytest.raises(PersistenceError):
            conversation.ledger.watermark()
    finally:
        store.close()
    with pytest.raises(PersistenceError):
        new_store = SessionStore(tmp_path).open()
        try:
            runtime = AgentRuntime(Model(), [])
            SavedConversation(new_store, runtime, runtime.llm.config, SessionStatus(tmp_path))
        finally:
            new_store.close()


@pytest.mark.parametrize("filename", ["snapshot", "index", "latest.json"])
def test_hard_exit_during_publication_repairs_new_session_selection(tmp_path, filename):
    script = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_execution_ledger import create, Write
from llm import Message
root = Path(sys.argv[1])
store, conversation, control = create(root, Write(root))
conversation.checkpoint(strict=True)
conversation.new_session(name="new selected session")
conversation.history = (Message("user", "committed before crash"),)
original = store.catalog._write
target = store.id + ".json" if sys.argv[2] == "snapshot" else sys.argv[2]
def crash(path, data):
    original(path, data)
    if path.name == target:
        os._exit(74)
store.catalog._write = crash
conversation.checkpoint(strict=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), filename],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 74, result.stderr.decode()
    store, conversation, control = create(tmp_path, Write(tmp_path))
    try:
        assert conversation.history[-1].content == "committed before crash"
        assert store.catalog.get(store.id)["name"] == "new selected session"
        assert store.catalog.latest_id() == store.id
        assert not (store.directory / ".pending-save").exists()
    finally:
        store.close()


def test_ledger_cli_reads_full_evidence_without_model_configuration(tmp_path, capsys):
    from cli.sessions_command import main

    store, conversation, control = create(tmp_path, Write(tmp_path, cancel=True))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        control.finish(ticket, control.execute(ticket))
        sid = store.id
    finally:
        store.close()
    main(["--root", str(tmp_path), "ledger", sid, "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2 and rows[0]["call_id"] == "write-1"
    assert rows[0]["observation"] and rows[0]["assistant"]


def test_ledger_transaction_failure_rolls_back_transition(tmp_path, monkeypatch):
    store, conversation, control = create(tmp_path, Write(tmp_path))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        original = conversation.ledger._event

        def fail(db, run, call, phase):
            if phase == "returned":
                raise OSError("result transaction aborted")
            original(db, run, call, phase)

        monkeypatch.setattr(conversation.ledger, "_event", fail)
        outcome = control.execute(ticket)
        assert outcome.kind == "error"
        control.finish(ticket, outcome)
        row = next(r for r in conversation.ledger.evidence() if r["call_id"] == "write-1")
        assert row["state"] == "started" and row["observation"] is None
        assert (tmp_path / "artifact").exists() and not (tmp_path / "later").exists()
    finally:
        store.close()


def test_unknown_cleanup_receipt_stops_before_next_tool(tmp_path):
    class Unclean(Write):
        execution_kind = Write.execution_kind

        def execute(self, arguments):
            result = super().execute(arguments)
            result.data["cleanup_status"] = "unknown"
            return result

    store, conversation, control = create(tmp_path, Unclean(tmp_path))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        outcome = control.execute(ticket)
        assert outcome.kind == "error" and outcome.cleanup_status == "unknown"
        control.finish(ticket, outcome)
        row = next(r for r in conversation.ledger.evidence() if r["call_id"] == "write-1")
        assert row["state"] == "returned"
        assert row["effects"]["result"]["cleanup_status"] == "unknown"
        assert not (tmp_path / "later").exists()
        with pytest.raises(ValueError, match="进程清理"):
            control.resume()
    finally:
        store.close()


def test_repaired_commit_precedes_external_rename(open_conversation, monkeypatch):
    first = open_conversation()
    original = first.store.catalog._write

    def fail(path, data):
        if path.name == "latest.json":
            raise OSError("pointer failure")
        original(path, data)

    monkeypatch.setattr(first.store.catalog, "_write", fail)
    with pytest.raises(OSError):
        first.checkpoint(strict=True)
    monkeypatch.setattr(first.store.catalog, "_write", original)
    first.store.catalog.rename(first.store.id, "external rename")
    first.checkpoint(strict=True)
    data = json.loads((first.store.directory / f"{first.store.id}.json").read_text())
    assert data["name"] == "external rename"
    assert not (first.store.directory / ".pending-save").exists()


@pytest.mark.parametrize("malformed", [{}, [], {"session_id": "../../escape"}])
def test_invalid_redo_record_is_preserved_and_never_published(open_conversation, malformed):
    first = open_conversation()
    path = first.store.directory / ".pending-save"
    snapshot = first.store.directory / f"{first.store.id}.json"
    before = snapshot.read_bytes()
    path.write_text(json.dumps(malformed))
    with pytest.raises(ValueError):
        first.store.catalog.get(first.store.id)
    assert snapshot.read_bytes() == before and path.exists()


def test_rebuilding_index_preserves_monotonic_commit_revision(open_conversation):
    first = open_conversation()
    first.checkpoint(strict=True)
    before = json.loads((first.store.directory / "index").read_text())["commit_revision"]
    (first.store.directory / "index").unlink()
    first.checkpoint(strict=True)
    after = json.loads((first.store.directory / "index").read_text())["commit_revision"]
    assert after > before


def test_ledger_pagination_keeps_latest_state_for_each_call(tmp_path):
    store, conversation, control = create(tmp_path, Write(tmp_path, cancel=True))
    try:
        control.enqueue("write")
        ticket = control.start_next()
        control.finish(ticket, control.execute(ticket))
        first = conversation.ledger.evidence(limit=1)[0]
        second = conversation.ledger.evidence(limit=1, before=first["seq"])[0]
        assert first["call_id"] == "write-1" and first["state"] == "returned"
        assert second["call_id"] == "write-2" and second["state"] == "intended"
        assert not conversation.ledger.evidence(before=second["seq"])
    finally:
        store.close()
