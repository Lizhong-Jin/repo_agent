"""Evidence, attribution and historical report contracts; no model/network needed."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.execution_ledger import ExecutionLedger
from agent.session import SessionStore
from agent.task_reports import (
    ReportCapture,
    ReportStore,
    changes,
    file_at,
    load_report,
    render_report,
    snapshot,
)
from cli.sessions_command import main, ui_command
from cli.task_controller import TaskController
from cli.task_execution import TaskOutcome
from host_support.change_evidence import DIFF_LIMIT, content_record, difference
from llm import Message, ToolCall
from tools import ToolResult
from tools.filesystem import (
    ApplyPatchTool,
    DeleteFileTool,
    MoveFileTool,
    WriteFileTool,
)


def begin(conversation, sandbox=None):
    task = conversation.queue.add("implement feature")
    conversation.queue.claim({})
    capture = ReportCapture(conversation, task, sandbox)
    conversation.ledger.begin(capture.report["attempt_id"], {"queue_task_id": task["id"]})
    return capture


def record(conversation, capture, call, execute):
    ledger, run = conversation.ledger, capture.report["attempt_id"]
    ledger.prepare(run, Message("assistant", tool_calls=[call]))
    ledger.start(run, call.id)
    capture.before_call(call)
    result = execute()
    ledger.finish(
        run, call.id, result.to_message(call), capture.after_call(call, result.effects.to_record())
    )
    return result


def finish(capture, state="completed"):
    capture.finish(state, {"writeback_ok": True, "cleanup_status": "not_needed"})
    return load_report(capture.store.store, capture.ledger)


def test_baseline_excludes_preexisting_edits_and_captures_untracked(open_conversation):
    c = open_conversation()
    root = c.store.project
    (root / "already.txt").write_text("user changes predating task")
    capture = begin(c)
    (root / "new.txt").write_text("external, no receipt")
    report = finish(capture)
    assert [change["path"] for change in report["changes"]] == ["new.txt"]
    assert report["changes"][0]["attribution"] == "unconfirmed"
    assert not report["operations"]
    assert "+external" in report["changes"][0]["diff"]


def test_controlled_write_followed_by_external_edit_keeps_both_diffs(open_conversation):
    c = open_conversation()
    root = c.store.project
    (root / "a.py").write_text("original\n")
    capture = begin(c)
    call = ToolCall("w", "write_file", {"path": "a.py", "content": "agent\n", "overwrite": True})
    result = record(c, capture, call, lambda call=call: WriteFileTool(root).execute(call.arguments))
    assert "file_changes" not in json.loads(result.to_message(call).content)["data"]
    (root / "a.py").write_text("concurrent user change\n")
    report = finish(capture)
    op = report["operations"][0]
    assert op["final_state"] == "changed_after"
    assert "+agent" in op["diff"] and "concurrent user" not in op["diff"]
    assert "+concurrent user change" in report["changes"][0]["diff"]
    assert c.ledger.read_result(op["ledger"]["result_ref"]) is not None
    (root / "a.py").write_text("changed long after delivery")
    assert load_report(c.store, c.ledger) == report


def test_net_zero_still_keeps_operation_history(open_conversation):
    c = open_conversation()
    root = c.store.project
    (root / "a").write_text("a")
    capture = begin(c)
    for index, content in enumerate(["b", "a"]):
        call = ToolCall(
            str(index), "write_file", {"path": "a", "content": content, "overwrite": True}
        )
        record(c, capture, call, lambda call=call: WriteFileTool(root).execute(call.arguments))
    report = finish(capture)
    assert report["changes"] == []
    assert len(report["operations"]) == 2
    assert report["operations"][-1]["final_state"] == "matches"


@pytest.mark.parametrize(
    "edit_when,expected",
    [(None, "unchanged"), ("during", "needs_recheck"), ("after", "needs_recheck")],
)
def test_verification_freshness(open_conversation, edit_when, expected):
    c = open_conversation()
    root = c.store.project
    (root / "a").write_text("v1")
    capture = begin(c)
    call = ToolCall("test", "run_command", {"command": ["pytest", "-q"], "cwd": "."})

    def execute():
        if edit_when == "during":
            (root / "a").write_text("v2")
        return ToolResult(True, {"exit_code": 0, "stdout": "12 passed", "stderr": ""})

    record(c, capture, call, execute)
    if edit_when == "after":
        (root / "a").write_text("v2")
    report = finish(capture)
    assert report["executions"][0]["state"] == "command_succeeded"
    assert report["executions"][0]["freshness"] == expected
    assert report["operations"] == []


@pytest.mark.parametrize(
    "data,state",
    [
        ({"exit_code": 2}, "failed"),
        ({"exit_code": 0, "timed_out": True}, "timed_out"),
        ({"exit_code": 0, "cleanup_status": "unknown"}, "unknown"),
    ],
)
def test_actual_process_result_wins_over_model_claims(open_conversation, data, state):
    c = open_conversation()
    capture = begin(c)
    record(
        c,
        capture,
        ToolCall("test", "run_command", {"command": ["pytest"]}),
        lambda: ToolResult(True, {**data, "stdout": "ALL TESTS PASSED"}),
    )
    report = finish(capture)
    assert report["executions"][0]["state"] == state


def test_interrupted_report_uses_ledger_without_rescanning(open_conversation, monkeypatch):
    c = open_conversation()
    capture = begin(c)
    call = ToolCall("w", "write_file", {"path": "a", "content": "saved"})
    record(c, capture, call, lambda: WriteFileTool(c.store.project).execute(call.arguments))

    # Simulate process loss before final snapshot/queue checkpoint.
    def forbidden(*args, **kwargs):
        raise AssertionError("must not rescan current project")

    monkeypatch.setattr("agent.task_reports.snapshot", forbidden)
    report = load_report(c.store, c.ledger)
    assert report["state"] == "incomplete"
    assert report["operations"][0]["final_state"] == "unknown"
    assert "final" not in report and "changes" not in report


def test_partial_patch_failure_lists_only_committed_operations(open_conversation, monkeypatch):
    c = open_conversation()
    root = c.store.project
    for name in ("a", "b"):
        (root / name).write_text("old\n")
    capture = begin(c)
    replace = Path.replace

    def fail(source, target):
        if target.name == "b":
            raise PermissionError("injected")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail)
    patch = (
        "*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n"
        "*** Update File: b\n@@\n-old\n+new\n*** End Patch"
    )
    call = ToolCall("patch", "apply_patch", {"patch": patch})
    result = record(c, capture, call, lambda: ApplyPatchTool(root).execute(call.arguments))
    assert not result.success
    report = finish(capture, "failed")
    assert [op["path"] for op in report["operations"]] == ["a"]
    assert report["failures"][0]["tool"] == "apply_patch"


def test_delete_and_move_are_not_invented_byte_writes(open_conversation):
    c = open_conversation()
    root = c.store.project
    (root / "a").write_text("data")
    capture = begin(c)
    call = ToolCall("move", "move_file", {"source": "a", "destination": "b"})
    assert record(c, capture, call, lambda: MoveFileTool(root).execute(call.arguments)).success
    call = ToolCall("del", "delete_file", {"path": "b"})
    record(c, capture, call, lambda: DeleteFileTool(root).execute(call.arguments))
    report = finish(capture)
    assert len(report["operations"]) == 3
    assert report["operations"][1]["final_state"] == "unknown"
    assert report["operations"][-1]["after"]["exists"] is False


def test_snapshot_limits_and_protected_paths(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("secret")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg").write_text("excluded")
    (tmp_path / "large").write_text("0123456789")
    monkeypatch.setattr("agent.task_reports.MAX_FILE_BYTES", 4)
    snap = snapshot(tmp_path)
    assert not snap["complete"]
    assert ".env" not in snap["files"]
    assert "node_modules/pkg" not in snap["files"]
    assert snap["files"]["large"]["unknown"]
    assert file_at(snap, "node_modules/pkg").get("unknown")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
def test_symlinks_cannot_import_external_content(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret").write_text("secret value")
    (root / "link").symlink_to(tmp_path / "secret")
    snap = snapshot(root)
    assert not snap["complete"] and "secret value" not in json.dumps(snap)


def test_binary_and_truncated_differences_are_explicit():
    assert (
        difference(content_record(b"\0"), content_record(b"\1"), "binary")["diff_status"]
        == "unavailable"
    )
    diff = difference(content_record(b"a" * 40000), content_record(b"b" * 40000), "long")
    assert diff["diff_status"] == "truncated" and len(diff["diff"]) == DIFF_LIMIT


def test_incomplete_snapshot_does_not_invent_deletions():
    before = {"files": {"a": content_record(b"old")}, "complete": True}
    after = {"files": {}, "complete": False}
    assert changes(before, after)[0]["kind"] == "unknown"


def test_reports_survive_clear_and_session_switch_and_export(open_conversation, capsys):
    c = open_conversation()
    capture = begin(c)
    finish(capture)
    sid = c.store.id
    c.clear()
    assert "任务交付报告 #1" in ui_command(c, "/report 1 --diff")
    c.new_session()
    with pytest.raises(ValueError, match="没有对应任务报告"):
        ui_command(c, "/report")
    store = SessionStore(c.store.project)
    store.id = sid
    assert load_report(store, ExecutionLedger(store))["task_number"] == 1
    capsys.readouterr()
    main(["--root", str(c.store.project), "report", sid, "--json"])
    assert json.loads(capsys.readouterr().out)["session_id"] == sid


def test_report_does_not_read_another_sessions_attempt(open_conversation):
    c = open_conversation()
    capture = begin(c)
    finish(capture)
    other = "f" * 32
    c.ledger.begin(other, {})
    call = ToolCall("other", "run_command", {"command": ["false"]})
    c.ledger.prepare(other, Message("assistant", tool_calls=[call]))
    c.ledger.start(other, call.id)
    c.ledger.finish(other, call.id, ToolResult(True, {"exit_code": 1}).to_message(call), {})
    assert not finish(capture)["executions"]


@pytest.mark.parametrize("failure", [None, "cancel", "error", "limit"])
def test_controller_always_saves_and_displays_report(open_conversation, failure):
    c = open_conversation()
    output = []
    controller = TaskController(c.runtime, conversation=c, write=output.append)
    controller.enqueue("do something")
    ticket = controller.start_next()
    outcome = controller.execute(ticket)
    if failure == "error":
        outcome = TaskOutcome("error", "injected")
    elif failure == "cancel":
        from host_support.cancellation import RunCancelled

        outcome = TaskOutcome("cancelled", RunCancelled(controller.cancellation))
    elif failure == "limit":
        from dataclasses import replace

        outcome.value = replace(outcome.value, status="max_steps")
    state = controller.finish(ticket, outcome)
    report = load_report(c.store, c.ledger)
    assert report["state"] == state
    assert any("任务交付报告 #1" in text for text in output)
    assert Path(c.queue.get(1)["result"]["report"]).is_file()


def test_report_failure_keeps_task_outcome_and_records_warning(open_conversation, monkeypatch):
    c = open_conversation()
    output = []
    controller = TaskController(c.runtime, conversation=c, write=output.append)
    controller.enqueue("task")
    ticket = controller.start_next()
    outcome = controller.execute(ticket)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ReportStore, "save", fail)
    assert controller.finish(ticket, outcome) == "completed"
    assert "disk full" in c.queue.get(1)["result"]["report_error"]
    assert load_report(c.store, c.ledger)["state"] == "incomplete"


def test_docker_reports_copy_and_host_separately(open_conversation, tmp_path):
    c = open_conversation()
    c.mode = "docker"
    workspace = tmp_path / "copy"
    workspace.mkdir()
    sandbox = SimpleNamespace(workspace=workspace, last_verification=None)
    capture = begin(c, sandbox)
    (workspace / "copy-only").write_text("pending apply")
    (c.store.project / "host-only").write_text("concurrent")
    report = finish(capture)
    assert [f["path"] for f in report["changes"]] == ["copy-only"]
    assert [f["path"] for f in report["host_changes"]] == ["host-only"]
    assert "宿主" in render_report(report, detail=True)


def test_runtime_receipts_capture_actual_command_and_ui_report(open_conversation):
    import subprocess
    import sys

    from agent import AgentRuntime
    from llm import LLMResponse, ToolDefinition
    from tools import ExecutionKind

    c = open_conversation()
    config = c.runtime.llm.config
    root = c.store.project

    class Check:
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = ToolDefinition("run_command", "test host command")

        def execute(self, arguments):
            completed = subprocess.run(
                arguments["command"], cwd=root, capture_output=True, text=True, timeout=10
            )
            return ToolResult(
                True,
                {
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
            )

    class Model:
        calls = 0

        def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                message = Message(
                    "assistant",
                    tool_calls=[
                        ToolCall("w", "write_file", {"path": "artifact", "content": "expected"})
                    ],
                )
            elif self.calls == 2:
                message = Message(
                    "assistant",
                    tool_calls=[
                        ToolCall(
                            "check",
                            "run_command",
                            {
                                "command": [
                                    sys.executable,
                                    "-c",
                                    "from pathlib import Path; "
                                    "assert Path('artifact').read_text() == 'expected'; "
                                    "print('verified')",
                                ]
                            },
                        )
                    ],
                )
            else:
                message = Message("assistant", "done")
            return LLMResponse(
                config.provider,
                config.model,
                message,
                finish_reason="tool_calls" if message.tool_calls else "stop",
            )

    model = Model()
    model.config = config
    c.runtime = AgentRuntime(model, [WriteFileTool(root), Check()])
    c.runtime.execution_ledger = c.ledger
    control = TaskController(c.runtime, conversation=c, write=lambda *_: None)
    control.enqueue("write and verify")
    ticket = control.start_next()
    assert control.finish(ticket, control.execute(ticket)) == "completed"
    report = load_report(c.store, c.ledger)
    assert report["operations"][0]["final_state"] == "matches"
    check = report["executions"][0]
    assert check["freshness"] == "unchanged"
    assert check["state"] == "command_succeeded" and "verified" in check["output_excerpt"]
    assert "run_command" in ui_command(c, "/report 1")


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink contract")
def test_report_read_rejects_symlink(open_conversation, tmp_path):
    c = open_conversation()
    capture = begin(c)
    report_file = capture.store.directory / (capture.report["attempt_id"] + ".json")
    report_file.unlink()
    other = tmp_path / "foreign.json"
    other.write_text("{}")
    report_file.symlink_to(other)
    with pytest.raises(OSError):
        load_report(c.store, c.ledger)


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory error injection")
def test_scan_failure_is_explicit(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        kwargs["onerror"](PermissionError("directory unreadable"))

    monkeypatch.setattr("agent.task_reports.os.fwalk", fail)
    result = snapshot(tmp_path)
    assert not result["complete"] and "directory unreadable" in result["issues"][0]
