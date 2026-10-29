"""Recovery must preserve uncertainty, never replay, and never mutate during diagnosis."""

import json
from copy import deepcopy

import pytest
from test_workspaces import isolated_git_config as isolated_git_config

from agent.recovery import Recovery, select_snapshot
from agent.session import SessionStore
from agent.task_queue import TaskQueue
from cli.recovery_command import main, recovery_lock
from cli.task_controller import TaskController


def contents(directory):
    return {
        # Windows byte-range locks forbid reading this file via another descriptor.
        # Still compare its existence and identity without opening its locked bytes.
        str(p.relative_to(directory)): (p.stat().st_ino, p.stat().st_size)
        if p == directory / ".lock"
        else p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


def pending_save(conversation, monkeypatch):
    original = conversation.store.catalog.complete_commit
    monkeypatch.setattr(
        conversation.store.catalog,
        "complete_commit",
        lambda *a: (_ for _ in ()).throw(OSError("injected publication loss")),
    )
    with pytest.raises(OSError):
        conversation.checkpoint(strict=True)
    monkeypatch.setattr(conversation.store.catalog, "complete_commit", original)


def test_read_only_snapshot_does_not_open_locked_bytes(open_conversation, monkeypatch):
    from pathlib import Path

    conversation = open_conversation()
    directory = conversation.store.directory
    read = Path.read_bytes

    def windows_read(path):
        if path == directory / ".lock":
            raise PermissionError("mandatory byte-range lock")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", windows_read)
    before = contents(directory)
    assert ".lock" in before
    assert Recovery(conversation.store).inspect()["plan"]
    assert contents(directory) == before
    (directory / "sentinel").write_bytes(b"changed")
    assert contents(directory) != before


@pytest.mark.parametrize("state", ["pending", "empty_pending", "running", "unhealthy", "ledger"])
@pytest.mark.parametrize("backend", ["local", "native", "docker"])
def test_startup_cannot_bypass_recovery_by_switching_backend(
    open_conversation, monkeypatch, state, backend
):
    from contextlib import ExitStack

    from cli import execution_environment as environment
    from cli.arguments import parse_arguments

    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    if state in {"pending", "empty_pending"}:
        data["pending_task"] = "unfinished" if state == "pending" else ""
    elif state == "running":
        queue = TaskQueue(data["task_queue"])
        queue.add("unfinished")
        queue.claim({})
        data["task_queue"] = queue.snapshot()
    elif state == "unhealthy":
        data["sandbox_healthy"] = False
    conversation.store.save(data)
    if state == "ledger":
        conversation.ledger.begin("newer-than-snapshot", {})
    monkeypatch.setattr(environment, "PermissionManager", lambda *a, **k: pytest.fail("startup"))
    _, args = parse_arguments(["--sandbox", backend])
    with ExitStack() as cleanup, pytest.raises(ValueError, match="recover plan"):
        environment.open_execution_environment(
            args, conversation.store.project, conversation.store, None, cleanup
        )


@pytest.mark.parametrize("pending", ["unfinished", ""])
def test_restart_keeps_cleanup_unknown_until_explicit_recovery(open_conversation, pending):
    conversation = open_conversation()
    conversation.pending_task = pending
    conversation.checkpoint(strict=True)
    conversation.store.close()
    for _ in range(2):
        restored = open_conversation()
        control = TaskController(restored.runtime, conversation=restored)
        assert restored.store.data["sandbox_healthy"] is False
        assert not control._healthy()
        with pytest.raises(ValueError, match="清理"):
            control.resume()
        assert not restored.runtime.llm.requests
        restored.store.close()


@pytest.mark.parametrize("state", ["intended", "started", "uncertain", "returned"])
def test_ledger_only_recovery_preserves_cleanup_uncertainty(open_conversation, state):
    from llm import Message, ToolCall

    conversation = open_conversation()
    ledger = conversation.ledger
    ledger.begin("lost-run", {})
    ledger.prepare(
        "lost-run", Message("assistant", tool_calls=[ToolCall("lost", "run_command", {})])
    )
    if state != "intended":
        ledger.start("lost-run", "lost")
    if state in {"uncertain", "returned"}:
        ledger.finish(
            "lost-run",
            "lost",
            Message("tool", "{}", tool_call_id="lost", name="run_command"),
            {},
            certain=state == "returned",
        )
    # More recent calls must not hide uncertainty beyond the display's 30-row limit.
    for index in range(35):
        ledger.prepare(
            "lost-run", Message("assistant", tool_calls=[ToolCall(str(index), "read_file", {})])
        )
    before = ledger.evidence()
    recovery = Recovery(conversation.store)
    result = recovery.apply(recovery.inspect()["plan"], "reconcile-session")
    assert ("confirm-cleanup" in result["actions"]) == (state in {"started", "uncertain"})
    assert ledger.evidence() == before
    _, evidence = select_snapshot(conversation.store, "latest")
    assert evidence["session"]["sandbox_healthy"] == (state in {"intended", "returned"})


def test_missing_workspace_record_never_falls_back_to_project(open_conversation, monkeypatch):
    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    data.update(workspace_id=conversation.store.id, sandbox_healthy=False)
    conversation.store.save(data)
    monkeypatch.setattr("agent.recovery.snapshot", lambda *a, **k: pytest.fail("project fallback"))
    report = Recovery(conversation.store).inspect()
    assert report["plan"] is None and not report["actions"]
    assert report["issues"][0]["code"] == "evidence.unreadable"


def test_session_number_takes_precedence_over_numeric_name(open_conversation):
    first = open_conversation()
    first_id = first.store.id
    first.store.close()
    second = open_conversation(new=True)
    second.rename("1")
    selected, _ = select_snapshot(second.store, "1")
    assert selected.id == first_id


def test_legacy_null_queue_does_not_block_healthy_restart(open_conversation):
    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    data["task_queue"] = None
    conversation.store.save(data)
    assert not conversation.ledger.recovery_required()
    conversation.store.close()
    restored = open_conversation()
    assert not restored.recovery_blocked and not restored.queue.data["tasks"]


def test_status_and_plan_never_replay_pending_commit(open_conversation, monkeypatch, capsys):
    conversation = open_conversation()
    pending_save(conversation, monkeypatch)
    store = conversation.store
    store.close()
    before = contents(store.directory)
    for action in ("status", "plan"):
        main([action, "--root", str(store.project), "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["actions"] == ["repair-save"]
        assert contents(store.directory) == before
    main(["apply", report["plan"], "--action", "repair-save", "--root", str(store.project)])
    assert not (store.directory / ".pending-save").exists()
    assert "repair-save" not in Recovery(store).inspect()["actions"]
    with pytest.raises(SystemExit) as error:
        main(["apply", report["plan"], "--action", "repair-save", "--root", str(store.project)])
    assert error.value.code == 1


def test_interrupt_reconciliation_preserves_history_and_waiting_work(open_conversation):
    conversation = open_conversation()
    queue = conversation.queue
    first = queue.add("first mutation")
    queue.add("waiting")
    queue.resume()
    queue.claim({})
    conversation.pending_task = first["text"]
    conversation.checkpoint(strict=True)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    assert "reconcile-session" in plan["actions"]
    recovery.apply(plan["plan"], "reconcile-session")
    _, data = select_snapshot(conversation.store, "latest")
    saved = data["session"]
    assert saved["task_queue"]["tasks"][0]["state"] == "interrupted"
    assert saved["task_queue"]["tasks"][1]["state"] == "queued"
    assert saved["task_queue"]["paused"] and saved["pending_task"] is None
    assert "不得自动重放" in saved["history"][-1]["content"]
    with pytest.raises(ValueError, match="过期"):
        recovery.apply(plan["plan"], "reconcile-session")


@pytest.mark.parametrize("change", ["file", "queue", "metadata", "receipt"])
def test_plan_rejects_changed_evidence(open_conversation, change):
    conversation = open_conversation()
    conversation.pending_task = "unfinished"
    conversation.checkpoint(strict=True)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    if change == "file":
        (conversation.store.project / "new.py").write_text("changed")
    elif change == "queue":
        conversation.queue.add("new task")
        conversation.checkpoint(strict=True)
    elif change == "metadata":
        conversation.store.catalog.rename(conversation.store.id, "renamed")
    else:
        from agent.workspaces import save_json

        save_json(conversation.store.directory / "recovery" / "prior.json", {"state": "started"})
    with pytest.raises(ValueError, match="过期"):
        recovery.apply(plan["plan"], "reconcile-session")


def test_active_project_lock_rejects_standalone_command(open_conversation):
    conversation = open_conversation()
    with pytest.raises(SystemExit) as error:
        main(["status", "--root", str(conversation.store.project)])
    assert error.value.code == 1
    control = TaskController(conversation.runtime, conversation=conversation)
    assert "恢复检查" in control.command("/recover")


def test_live_checkpoint_preserves_memory_and_pauses_queue(open_conversation):
    conversation = open_conversation()
    control = TaskController(conversation.runtime, conversation=conversation)
    control.queue.add("not yet saved")
    control.persistence_blocked = True
    recovery = Recovery(conversation.store, conversation.store.id, controller=control)
    plan = recovery.inspect()
    assert plan["actions"] == ["checkpoint"]
    control.command(f"/recover apply {plan['plan']} checkpoint")
    assert not control.persistence_blocked and control.queue.paused
    assert conversation.store.data["task_queue"]["tasks"][0]["text"] == "not yet saved"


def test_running_controller_cannot_repair(open_conversation):
    conversation = open_conversation()
    control = TaskController(conversation.runtime, conversation=conversation)
    control.persistence_blocked, control.active = True, {"id": "running"}
    report = Recovery(conversation.store, conversation.store.id, controller=control).inspect()
    assert not report["actions"]
    assert any(row["code"] == "execution.active" for row in report["issues"])


def test_cleanup_requires_explicit_scoped_confirmation_and_keeps_old_results(open_conversation):
    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    queue = TaskQueue(data["task_queue"])
    task = queue.add("cancelled execution")
    queue.resume()
    queue.claim({})
    queue.finish(task, "failed", {"cleanup_status": "unknown"})
    data.update(sandbox_healthy=False, task_queue=queue.snapshot())
    conversation.store.save(data)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    with pytest.raises(ValueError, match="confirm-stopped"):
        recovery.apply(plan["plan"], "confirm-cleanup")
    recovery.apply(
        plan["plan"], "confirm-cleanup", confirm_stopped=True, note="核实本次执行及全部子进程已停止"
    )
    _, evidence = select_snapshot(conversation.store, "latest")
    assert evidence["session"]["sandbox_healthy"]
    assert evidence["session"]["task_queue"]["tasks"][0]["result"]["cleanup_status"] == "unknown"
    assert evidence["session"]["task_queue"]["paused"]
    assert evidence["session"]["recovery_cleanup"]["basis"] == "user_confirmation"


def test_corrupt_or_symlinked_records_fail_closed(open_conversation, tmp_path):
    conversation = open_conversation()
    path = conversation.store.directory / (conversation.store.id + ".json")
    original = path.read_bytes()
    path.write_text("broken")
    report = Recovery(conversation.store).inspect()
    assert report["plan"] is None and not report["actions"]
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_bytes(original)
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert Recovery(conversation.store).inspect()["plan"] is None
    assert outside.read_bytes() == original


def test_recovery_restart_after_receipt_publication_failure(open_conversation, monkeypatch):
    import agent.recovery as module

    conversation = open_conversation()
    conversation.pending_task = "old task"
    conversation.checkpoint(strict=True)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    save = module.save_json

    def fail_finish(path, data):
        if data.get("state") == "completed":
            raise OSError("power loss after state save")
        return save(path, data)

    monkeypatch.setattr(module, "save_json", fail_finish)
    with pytest.raises(OSError):
        recovery.apply(plan["plan"], "reconcile-session")
    assert "reconcile-session" not in recovery.inspect()["actions"]
    with pytest.raises(ValueError, match="过期"):
        recovery.apply(plan["plan"], "reconcile-session")


def test_cli_routes_recovery_before_model_configuration(open_conversation, monkeypatch, capsys):
    import cli.main as entry

    conversation = open_conversation()
    store = conversation.store
    store.close()
    monkeypatch.setattr(entry, "run_application", lambda *a, **k: pytest.fail("model startup"))
    entry.main(["--root", str(store.project), "recover", "status"])
    assert "恢复检查" in capsys.readouterr().out


def test_missing_project_state_does_not_create_files(tmp_path):
    project = tmp_path / "new"
    project.mkdir()
    store = SessionStore(project)
    with pytest.raises(ValueError):
        with recovery_lock(store):
            pytest.fail("no existing lock")
    assert not store.directory.exists()


def test_reconcile_ledger_ahead_keeps_original_receipts(open_conversation):
    from llm import Message, ToolCall

    conversation = open_conversation()
    ledger = conversation.ledger
    ledger.begin("interrupted-run", {})
    ledger.prepare(
        "interrupted-run",
        Message("assistant", tool_calls=[ToolCall("call", "read_file", {"path": "a"})]),
    )
    before = ledger.evidence()
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    assert any(row["code"] == "ledger.ahead" for row in plan["issues"])
    recovery.apply(plan["plan"], "reconcile-session")
    assert ledger.evidence() == before
    assert "reconcile-session" not in recovery.inspect()["actions"]


def test_missing_required_ledger_blocks_all_actions(open_conversation):
    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    data["ledger_cursor"] = 9
    conversation.store.save(data)
    report = Recovery(conversation.store).inspect()
    assert not report["actions"] and report["plan"] is None


def test_incomplete_file_scan_blocks_mutations(open_conversation, monkeypatch):
    conversation = open_conversation()
    conversation.pending_task = "unfinished"
    conversation.checkpoint(strict=True)
    monkeypatch.setattr(
        "agent.recovery.snapshot", lambda *a, **k: {"files": {}, "complete": False, "at": "now"}
    )
    report = Recovery(conversation.store).inspect()
    assert not report["actions"]
    assert any(row["code"] == "content.incomplete" for row in report["issues"])


def test_workspace_recovery_uses_real_git_without_replaying_merge(tmp_path, monkeypatch):
    from agent.workspaces import Workspace, prepare_workspace
    from host_support.git_worktree import Git

    root = tmp_path / "git-project"
    root.mkdir()
    (root / "file").write_text("baseline")
    git = Git(root)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("add", "file")
    git.run("commit", "-m", "baseline")
    store = SessionStore(root).open()
    try:
        workspace = prepare_workspace(store, "worktree")
        workspace.data.update(state="merging", merge_commit=git.head())
        workspace.save()
        recovery = Recovery(store, store.id)
        before = contents(store.directory)
        plan = recovery.inspect()
        assert "workspace-recover" in plan["actions"]
        assert contents(store.directory) == before
        monkeypatch.setattr(Workspace, "merge", lambda *a: pytest.fail("never merge again"))
        recovery.apply(plan["plan"], "workspace-recover")
        assert Workspace(store).data["state"] == "merged"
    finally:
        store.close()


@pytest.mark.parametrize("damage", ["missing", "replacement"])
def test_workspace_cleanup_rejects_lost_git_identity(tmp_path, damage):
    from agent.workspaces import prepare_workspace
    from host_support.git_worktree import Git

    root = tmp_path / "git-project"
    root.mkdir()
    (root / "file").write_text("baseline")
    git = Git(root)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("add", "file")
    git.run("commit", "-m", "baseline")
    store = SessionStore(root).open()
    try:
        workspace = prepare_workspace(store, "worktree")
        workspace.data["state"] = "blocked"
        workspace.save()
        recovery = Recovery(store, store.id)
        plan = recovery.inspect()
        assert "confirm-cleanup" in plan["actions"]
        workspace.root.rename(workspace.root.with_name("retained"))
        if damage == "replacement":
            workspace.root.mkdir()
        report = recovery.inspect()
        assert report["plan"] is None and not report["actions"]
        with pytest.raises(ValueError, match="过期"):
            recovery.apply(
                plan["plan"], "confirm-cleanup", confirm_stopped=True, note="old observation"
            )
        assert workspace.data["state"] == "blocked"
    finally:
        store.close()


def test_strict_recovery_cannot_turn_interrupted_task_into_delivery_success(open_conversation):
    conversation = open_conversation()
    data = deepcopy(conversation.store.data)
    data["delivery_policy"] = {
        "version": 1,
        "mode": "strict",
        "checks": [
            {"id": "review", "kind": "manual", "title": "review", "expectation": "check changes"}
        ],
    }
    queue = TaskQueue(data["task_queue"])
    queue.add("unfinished")
    queue.resume()
    task = queue.claim({})
    queue.data["delivery_latest"] = task["id"]
    data.update(task_queue=queue.snapshot(), pending_task="unfinished")
    conversation.store.save(data)
    recovery = Recovery(conversation.store)
    plan = recovery.inspect()
    assert "refresh-delivery" not in plan["actions"]
    report = recovery.apply(plan["plan"], "reconcile-session")
    assert "confirm-cleanup" in report["actions"]
    assert "refresh-delivery" not in report["actions"]
    report = recovery.apply(
        report["plan"], "confirm-cleanup", confirm_stopped=True, note="所有进程已停止"
    )
    report = recovery.apply(report["plan"], "refresh-delivery")
    _, evidence = select_snapshot(conversation.store, "latest")
    task = evidence["session"]["task_queue"]["tasks"][0]
    assert task["state"] == "interrupted" and task["result"]["delivery"]["state"] == "unknown"


def test_line_and_tui_recovery_commands_do_not_call_model(open_conversation, monkeypatch, capsys):
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.output import DummyOutput
    from test_interactive import inputs

    from cli.interactive import run_interactive
    from cli.terminal.application import ConversationUI

    conversation = open_conversation()
    conversation.queue.pause("waiting for review")
    monkeypatch.setattr(conversation.runtime, "run", lambda *a, **k: pytest.fail("model called"))
    inputs(monkeypatch, ["/recover", "/exit"])
    run_interactive(conversation.runtime, conversation=conversation)
    assert "恢复检查" in capsys.readouterr().out
    ui = ConversationUI(
        conversation.runtime,
        conversation=conversation,
        terminal_input=DummyInput(),
        terminal_output=DummyOutput(),
    )
    monkeypatch.setattr(ui, "_start_next", lambda: pytest.fail("diagnosis dispatched queue"))
    ui.editor.text = "/recover"
    ui.submit()
    assert "恢复检查" in ui.transcript


def test_remote_cli_failure_sanitizes_errors_and_preserves_evidence(
    open_conversation, monkeypatch, capsys
):
    from test_external_operations import pr_args

    from tools._internal.github_service import GitHubService
    from tools._internal.operation_journal import OperationJournal

    conversation = open_conversation()
    journal = OperationJournal(conversation.store.directory, conversation.store.id)
    record = journal.begin(pr_args())
    journal.update(record, stage="mutation_requested")
    plan = Recovery(conversation.store).inspect()
    conversation.store.close()
    monkeypatch.setattr(
        GitHubService, "verify", lambda *a: (_ for _ in ()).throw(RuntimeError("secret-token"))
    )
    with pytest.raises(SystemExit) as error:
        main(
            [
                "apply",
                plan["plan"],
                "--root",
                str(conversation.store.project),
                "--action",
                "verify:" + record["id"],
                "--allow-network",
            ]
        )
    assert error.value.code == 1 and "secret-token" not in capsys.readouterr().err
    assert journal.read(record["id"]) == record
