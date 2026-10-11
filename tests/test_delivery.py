"""User-owned requirements, durable delivery gates and real Git merge boundaries."""

import json
from contextlib import closing
from copy import deepcopy

import pytest
from session_helpers import Model
from test_task_reports import record
from test_verification_plan import command_item
from test_workspaces import isolated_git_config  # noqa: F401

from agent import AgentRuntime, delivery
from agent.conversation import SavedConversation
from agent.report_reviews import record_review
from agent.session import SessionStore
from agent.task_reports import ReportStore, load_report
from agent.verification import PlanVerificationTool
from agent.workspaces import Workspace, prepare_workspace
from cli.arguments import parse_arguments
from cli.delivery_command import main as delivery_main
from cli.session_status import SessionStatus
from cli.task_controller import TaskController
from host_support.git_worktree import Git
from llm import ToolCall
from tools import ToolResult


@pytest.fixture
def factory(tmp_path):
    stores = []

    def create(*, checks=None, worktree=False, mode="strict"):
        root = tmp_path / f"project-{len(stores)}"
        root.mkdir()
        (root / "a.py").write_text("original\n")
        if worktree:
            git = Git(root)
            git.run("init", "--template=", "--initial-branch=main")
            git.run("add", "a.py")
            git.run("commit", "-m", "baseline")
        store = SessionStore(root).open()
        stores.append(store)
        workspace = prepare_workspace(store, "worktree") if worktree else None
        policy = delivery.validate_policy(
            {
                "version": 1,
                "mode": mode,
                "checks": (checks if checks is not None else [command_item()])
                if mode == "strict"
                else [],
            }
        )
        model = Model()
        status = SessionStatus(root, context_window=1000)
        runtime = AgentRuntime(model, on_event=status)
        c = SavedConversation(
            store, runtime, model.config, status, workspace=workspace, delivery_policy=policy
        )
        c.checkpoint(strict=True)
        control = TaskController(
            runtime, conversation=c, write=lambda *_: None, write_model=lambda *_: None
        )
        return c, control

    yield create
    for store in stores:
        store.close()


def start(control):
    if not control.queue.pending:
        control.enqueue("complete feature")
    task = control.start_next()
    assert task is not None
    outcome = control.execute(task)
    return task, outcome


def command(c, control, *, code=0, command=None, call_id="test"):
    call = ToolCall(
        call_id, "run_command", {"command": command or ["python", "check.py"], "cwd": "."}
    )
    return record(
        c,
        control.report_capture,
        call,
        lambda: ToolResult(True, {"exit_code": code, "cleanup_status": "confirmed"}),
    )


def finish(c, control, *, code=0, run=True):
    task, outcome = start(control)
    if run:
        command(c, control, code=code)
    assert control.finish(task, outcome) == "completed"
    return control.queue.get(task["number"])


def test_policy_requires_explicit_file_and_freezes_session(factory, tmp_path):
    c, _ = factory()
    policy = tmp_path / "checks.json"
    policy.write_text(json.dumps({"version": 1, "checks": [command_item()]}))
    assert delivery.resolve_policy(c.store, path=policy) == c.delivery_policy
    policy.write_text(
        json.dumps({"version": 1, "checks": [{**command_item(), "command": ["true"]}]})
    )
    with pytest.raises(ValueError, match="已经固定"):
        delivery.resolve_policy(c.store, path=policy)
    assert delivery.resolve_policy(c.store) == c.delivery_policy
    with pytest.raises(ValueError):
        delivery.resolve_policy(c.store, "report-only")
    _, args = parse_arguments(["--delivery", "strict", "--delivery-policy", str(policy)])
    assert args.delivery == "strict"


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [command_item(), command_item()],
        [{**command_item(), "state": "passed"}],
        [{**command_item(), "cwd": "../x"}],
    ],
)
def test_invalid_required_contract_rejected(checks):
    with pytest.raises(ValueError):
        delivery.validate_policy({"version": 1, "mode": "strict", "checks": checks})


def test_missing_required_check_blocks_completed_task_and_resume(factory):
    c, control = factory()
    control.enqueue("first")
    control.enqueue("next")
    task = finish(c, control, run=False)
    assert task["state"] == "completed"
    assert control.delivery_result()["state"] == "blocked"
    assert "保存的交付结论：交付受阻" in control.describe()
    assert control.queue.paused and control.start_next() is None
    with pytest.raises(ValueError, match="不能直接恢复"):
        control.resume()
    assert "host-required" == load_report(c.store, c.ledger)["verification_items"][0]["call_id"]


def test_passed_checks_allow_queue_but_new_task_cannot_borrow_evidence(factory):
    c, control = factory()
    control.enqueue("first")
    control.enqueue("second")
    finish(c, control)
    assert control.delivery_allowed() and not control.queue.paused
    finish(c, control, run=False)
    assert control.delivery_result()["state"] == "blocked"


def test_model_cannot_redefine_host_requirement(factory):
    c, control = factory()
    task, outcome = start(control)
    tool = PlanVerificationTool(lambda: control.report_capture.required_plans)
    changed = {**command_item(), "command": ["true"]}
    assert not tool.execute({"items": [changed]}).success
    assert tool.execute({"items": [command_item()]}).data["registered"] == 0
    control.finish(task, outcome)
    assert control.delivery_result()["state"] == "blocked"


@pytest.mark.parametrize("when", ["during_task", "after_task"])
def test_content_changes_invalidate_checks_and_cannot_be_waived(factory, when):
    c, control = factory()
    task, outcome = start(control)
    command(c, control)
    if when == "during_task":
        (c.execution_root / "a.py").write_text("changed")
    control.finish(task, outcome)
    if when == "after_task":
        (c.execution_root / "a.py").write_text("changed")
    assert control.delivery_result()["state"] == "stale"
    with pytest.raises(ValueError, match="先恢复"):
        control.command("/delivery accept-exception skip")


def test_exception_keeps_failure_and_is_bound_to_report_and_content(factory):
    c, control = factory()
    task = finish(c, control, code=1)
    before = ReportStore(c.store).get()
    old = task["result"]["delivery"]["file"]
    control.command("/delivery accept-exception known failing fixture")
    assert control.delivery_result()["state"] == "accepted_exception"
    assert ReportStore(c.store).get() == before
    assert load_report(c.store, c.ledger)["verification_items"][0]["state"] == "failed"
    assert (delivery.directory(c.store) / old).exists()
    control.resume()
    (c.execution_root / "a.py").write_text("later change")
    assert not control.delivery_allowed()


def test_manual_review_needs_refresh_and_does_not_override_failed_command(factory):
    manual = {"id": "ui", "title": "UI", "kind": "manual", "expectation": "review UI"}
    c, control = factory(checks=[command_item(), manual])
    finish(c, control)
    assert control.delivery_result()["state"] == "blocked"
    record_review(c.store, "1", "ui", "accepted", "checked")
    assert control.delivery_result()["state"] == "unknown"
    control.command("/delivery refresh")
    assert control.delivery_allowed()
    record_review(c.store, "1", "unit", "rejected", "missing coverage")
    assert not control.delivery_allowed()
    control.command("/delivery refresh")
    assert control.delivery_result()["state"] == "blocked"


def test_recheck_goes_first_and_keeps_old_report(factory):
    c, control = factory()
    control.enqueue("first")
    control.enqueue("waiting")
    old = finish(c, control, code=1)
    old_report = ReportStore(c.store).get(1)
    control.command("/delivery recheck")
    assert control.queue.pending[0]["delivery_recovery_of"] == old["id"]
    new = finish(c, control)
    assert new["number"] == 3
    assert control.delivery_allowed()  # Execution order, not task-number ordering.
    finish(c, control, run=False)  # Task #2 executes last and must now block.
    assert control.delivery_result()["state"] == "blocked"
    assert ReportStore(c.store).get(1) == old_report
    assert ReportStore(c.store).get()["task_number"] == 2


@pytest.mark.parametrize("failure", ["report", "decision", "session"])
def test_publication_failure_never_advances_queue(factory, monkeypatch, failure):
    c, control = factory()
    control.enqueue("first")
    control.enqueue("waiting")
    task, outcome = start(control)
    command(c, control)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        if failure == "report":
            m.setattr(control.report_capture.store, "save", fail)
        elif failure == "decision":
            m.setattr(delivery, "atomic_write", fail)
        else:
            m.setattr(c.store, "save", fail)
        if failure == "session":
            with pytest.raises(OSError):
                control.finish(task, outcome)
        else:
            control.finish(task, outcome)
        assert control.queue.paused
        assert control.start_next() is None
    if failure != "session":
        assert not control.delivery_allowed()
    if failure == "decision":
        control.command("/delivery refresh")
        assert control.delivery_allowed()


def test_strict_baseline_failure_does_not_call_model(factory, monkeypatch):
    c, control = factory()
    control.enqueue("first")
    task = control.start_next()
    monkeypatch.setattr(ReportStore, "save", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    outcome = control.execute(task)
    assert outcome.kind == "error"
    assert not c.runtime.llm.requests


def test_cleanup_unknown_cannot_accept_exception_or_recheck(factory):
    c, control = factory()
    task, outcome = start(control)
    command(c, control)
    outcome.cleanup_status = "unknown"
    control.finish(task, outcome)
    assert not control.delivery_allowed()
    for action in ["/delivery recheck", "/delivery accept-exception proceed", "/delivery refresh"]:
        with pytest.raises(ValueError, match="清理未确认"):
            control.command(action)


def test_report_tampering_and_missing_decision_fail_closed(factory):
    c, control = factory()
    task = finish(c, control)
    path = delivery.directory(c.store) / task["result"]["delivery"]["file"]
    original = path.read_bytes()
    path.unlink()
    assert control.delivery_result()["state"] == "unknown"
    path.write_bytes(original)
    report = ReportStore(c.store).get()
    report["unverified"].append("later update")
    ReportStore(c.store).save(report)
    assert control.delivery_result()["state"] == "unknown"


def test_restart_restores_frozen_policy_and_gate(factory):
    c, control = factory()
    finish(c, control, code=1)
    project = c.store.project
    c.store.close()
    with closing(SessionStore(project).open()) as store:
        model = Model()
        status = SessionStatus(project, context_window=1000)
        runtime = AgentRuntime(model, on_event=status)
        restored = SavedConversation(store, runtime, model.config, status)
        other = TaskController(runtime, conversation=restored, write=lambda *_: None)
        assert restored.delivery_policy == c.delivery_policy
        with pytest.raises(ValueError, match="不能直接恢复"):
            other.resume()
        assert other.delivery_result()["state"] == "blocked"


def test_interrupted_session_cannot_reinterpret_completed_report(factory):
    c, control = factory()
    task = finish(c, control)
    task["state"] = "interrupted"
    assert control.delivery_result()["state"] == "unknown"
    control.command("/delivery refresh")
    assert not control.delivery_allowed()


def test_report_only_legacy_session_keeps_old_behavior(factory):
    c, control = factory(mode="report-only")
    finish(c, control, run=False)
    assert control.delivery_allowed() and not control.queue.paused
    data = deepcopy(c.store.data)
    data.pop("delivery_policy")
    c.store.data = data
    assert delivery.resolve_policy(c.store) == delivery.DEFAULT


def test_merge_requires_verified_current_candidate(factory):
    c, control = factory(worktree=True)
    (c.execution_root / "a.py").write_text("feature\n")
    finish(c, control, code=1)
    review = c.workspace.review()
    with pytest.raises(ValueError, match="严格交付禁止合并"):
        c.workspace.merge(review["token"])
    assert (c.store.project / "a.py").read_text() == "original\n"
    control.command("/delivery recheck")
    finish(c, control)
    review = c.workspace.review()
    commit = c.workspace.merge(review["token"])
    assert Git(c.store.project).head() == commit
    assert (c.store.project / "a.py").read_text() == "feature\n"


def test_fresh_review_token_does_not_validate_new_content(factory):
    c, control = factory(worktree=True)
    finish(c, control)
    (c.execution_root / "a.py").write_text("not tested\n")
    review = c.workspace.review()
    with pytest.raises(ValueError, match="过期"):
        c.workspace.merge(review["token"])


def test_git_tree_covers_tracked_files_excluded_from_report_scan(factory):
    c, control = factory(worktree=True)
    target = c.execution_root / "build"
    target.mkdir()
    (target / "tracked.txt").write_text("one")
    git = Git(c.execution_root)
    git.run("add", "build/tracked.txt")
    git.run("commit", "-m", "tracked generated file")
    finish(c, control)
    (target / "tracked.txt").write_text("two")
    assert control.delivery_result()["state"] == "stale"


def test_pinned_worktree_policy_prevents_missing_session_bypass(factory):
    c, control = factory(worktree=True)
    finish(c, control)
    review = c.workspace.review()
    path = c.store.directory / f"{c.store.id}.json"
    path.unlink()
    with pytest.raises(ValueError):
        Workspace(c.store).merge(review["token"])


def test_offline_user_exception_does_not_execute_commands(factory, capsys):
    c, control = factory(worktree=True)
    finish(c, control, code=1)
    project = str(c.store.project)
    sid = c.store.id
    calls = len(c.runtime.llm.requests)
    c.store.close()
    delivery_main(
        ["accept-exception", "--root", project, "--session", sid, "--note", "known test issue"]
    )
    assert "用户接受例外" in capsys.readouterr().out
    assert len(c.runtime.llm.requests) == calls
    with closing(SessionStore(project, session=sid).open()) as store:
        workspace = Workspace(store)
        workspace.merge(workspace.review()["token"])


def test_end_to_end_runtime_records_real_command_without_model_plan(factory):
    import subprocess
    import sys

    from test_runtime import ScriptedLLM, reply

    from llm import ToolDefinition
    from tools import ExecutionKind

    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('a.py').read_text() == 'original\\n'",
    ]
    c, _ = factory(checks=[{**command_item(), "command": argv}])

    class Check:
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = ToolDefinition("run_command", "trusted test fixture")

        def execute(self, arguments):
            completed = subprocess.run(
                arguments["command"], cwd=c.execution_root, capture_output=True, timeout=10
            )
            return ToolResult(
                True, {"exit_code": completed.returncode, "cleanup_status": "confirmed"}
            )

    model = ScriptedLLM(
        [reply(calls=[ToolCall("check", "run_command", {"command": argv})]), reply("done")]
    )
    model.config = c.config
    runtime = AgentRuntime(model, [Check()])
    runtime.execution_ledger = c.ledger
    c.runtime = runtime
    control = TaskController(
        runtime, conversation=c, write=lambda *_: None, write_model=lambda *_: None
    )
    task, outcome = start(control)
    control.finish(task, outcome)
    assert control.delivery_result()["state"] == "passed"
    report = load_report(c.store, c.ledger)
    assert report["delivery_record"]["state"] == "passed"
    assert report["verification_items"][0]["attempts"][0]["ledger"]["result_ref"]


def test_clock_rollback_cannot_select_an_older_pass(factory, monkeypatch):
    from agent import task_queue

    c, control = factory()
    finish(c, control)
    monkeypatch.setattr(task_queue, "now", lambda: "2000-01-01T00:00:00+00:00")
    finish(c, control, run=False)
    assert control.delivery_result()["state"] == "blocked"


def test_incomplete_scan_never_counts_as_pass_or_exception(factory, monkeypatch):
    import agent.task_reports as reports

    c, control = factory()
    finish(c, control)
    original = reports.snapshot

    def incomplete(*args, **kwargs):
        value = original(*args, **kwargs)
        value["complete"] = False
        return value

    monkeypatch.setattr(reports, "snapshot", incomplete)
    assert not control.delivery_allowed()
    with pytest.raises(ValueError, match="先恢复"):
        control.command("/delivery accept-exception skip")
