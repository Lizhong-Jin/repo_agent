"""Explicit session selection and UI handoff preserve independent persistent state."""

import asyncio
import json
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from session_helpers import Model, perform

from agent.session import SessionStore
from cli import application, main, runtime_setup
from cli.arguments import parse_arguments
from cli.execution_environment import ExecutionEnvironment
from cli.session_switch import SessionSwitch, continuation_args, prepare_switch
from cli.task_controller import TaskController
from cli.terminal.application import ConversationUI
from llm import Message


@pytest.fixture
def pair(open_conversation):
    c = open_conversation()
    c.rename("first session")
    perform(c, "remember first")
    first = c.store.id
    c.new_session(name="second session")
    original = json.loads((c.store.directory / f"{first}.json").read_text())
    perform(c, "remember second")
    perform(c, "another second task")
    return c, first, original


@pytest.mark.parametrize("selector", ["1", "first session", "id"])
def test_explicit_store_selection_does_not_publish_until_save(pair, selector):
    c, first, original = pair
    project, second = c.store.project, c.store.id
    directory = c.store.directory
    c.store.close()
    target = SessionStore(project, session=first if selector == "id" else selector).open()
    try:
        assert target.id == first and target.data == original
        assert target.catalog.latest_id() == second
        assert (directory / f"{second}.json").exists()
        target.save(target.data)
        assert target.catalog.latest_id() == first
    finally:
        target.close()
    restored = SessionStore(project).open()
    assert restored.id == first
    restored.close()


@pytest.mark.parametrize(
    "options",
    [
        ["--session", "1", "--new-session"],
        ["--session", "1", "--name", "new"],
        ["--session", " "],
    ],
)
def test_cli_rejects_conflicting_or_empty_selector(options):
    with pytest.raises(SystemExit) as caught:
        parse_arguments(options)
    assert caught.value.code == 2


@pytest.mark.parametrize("selector", ["missing", "../other", "f" * 32])
def test_invalid_selector_keeps_pointer_and_releases_lock(pair, selector):
    c, _, _ = pair
    project, old = c.store.project, c.store.id
    c.store.close()
    with pytest.raises(ValueError, match="找不到会话"):
        SessionStore(project, session=selector).open()
    restored = SessionStore(project).open()
    assert restored.id == old
    restored.close()


def test_ambiguous_name_and_foreign_project_cannot_be_selected(pair, open_conversation, tmp_path):
    c, first, _ = pair
    c.store.catalog.rename(first, "second session")
    with pytest.raises(ValueError, match="同名"):
        c.store.read_session("second session")
    foreign = open_conversation(tmp_path / "another")
    with pytest.raises(ValueError, match="找不到会话"):
        c.store.read_session(foreign.store.id)


def test_corrupt_target_is_read_without_changing_source(pair):
    c, first, _ = pair
    current, data = c.store.id, c.store.data
    (c.store.directory / f"{first}.json").write_text('{"invalid": true}')
    with pytest.raises(ValueError, match="无法恢复"):
        c.store.select(first)
    assert c.store.id == current and c.store.data is data
    assert c.store.catalog.latest_id() == current


def test_prepare_switch_checks_idle_and_keeps_paused_queue(pair):
    c, first, _ = pair
    controller = TaskController(c.runtime, conversation=c)
    controller.enqueue("queued source work")
    with pytest.raises(ValueError, match="pause"):
        prepare_switch(c, controller, "/switch 1")
    controller.pause()
    controller.active = {"id": "in-progress"}
    with pytest.raises(ValueError, match="收尾"):
        prepare_switch(c, controller, "/switch 1")
    controller.active = None
    controller.cleanup_blocked = True
    with pytest.raises(ValueError, match="清理"):
        prepare_switch(c, controller, "/switch 1")
    controller.cleanup_blocked = False
    result = prepare_switch(c, controller, '/switch "first session"')
    assert result == SessionSwitch(first)
    assert c.store.id != first
    assert c.store.data["task_queue"]["paused"]
    assert controller.queue.pending[0]["text"] == "queued source work"


def test_noop_and_malformed_switch_do_not_save(pair, monkeypatch):
    c, _, _ = pair
    controller = TaskController(c.runtime, conversation=c)
    monkeypatch.setattr(c, "checkpoint", lambda **kw: pytest.fail("unexpected write"))
    assert prepare_switch(c, controller, "/switch 2") is None
    with pytest.raises(ValueError, match="用法"):
        prepare_switch(c, controller, "/switch")
    with pytest.raises(ValueError):
        prepare_switch(c, controller, '/switch "unfinished')


def test_save_failure_prevents_handoff(pair, monkeypatch):
    c, _, _ = pair
    current = c.store.id

    def fail(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(c, "checkpoint", fail)
    with pytest.raises(OSError, match="disk full"):
        prepare_switch(c, TaskController(c.runtime, conversation=c), "/switch 1")
    assert c.store.id == current and c.store.catalog.latest_id() == current


def mock_models(monkeypatch):
    clients = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic")
    monkeypatch.setattr(application.WebBackend, "from_environment", lambda: None)

    def factory(config):
        client = Model(config)
        client.close = lambda: None
        clients.append(client)
        return nullcontext(client)

    monkeypatch.setattr(runtime_setup, "LLMClient", factory)
    return clients


def launch(project, *extra):
    main.main(["--root", str(project), "--sandbox", "local", "--model", "m", *extra])


def test_real_line_switch_and_startup_selection_separate_history_logs_and_usage(
    pair, monkeypatch, capsys
):
    c, first, original = pair
    project, second = c.store.project, c.store.id
    c.store.close()
    clients = mock_models(monkeypatch)
    inputs = iter(["/switch 1", "after switch first", "/switch 2", "after switch second", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    launch(project)
    assert len(clients) == 3
    assert not clients[0].requests
    first_contents = [m.content for m in clients[1].requests[0].messages]
    second_contents = [m.content for m in clients[2].requests[0].messages]
    assert "remember first" in first_contents and "remember second" not in first_contents
    assert "remember second" in second_contents and "after switch first" not in second_contents
    catalog = SessionStore(project).catalog
    first_data = json.loads((catalog.directory / f"{first}.json").read_text())
    second_data = json.loads((catalog.directory / f"{second}.json").read_text())
    assert first_data["status"]["calls"] == original["status"]["calls"] + 1
    assert second_data["status"]["calls"] == 3
    assert first_data["task_number"] == 2 and second_data["task_number"] == 4  # /new keeps counter
    assert catalog.latest_id() == second
    assert "after switch first" not in catalog.log_path(second).read_text()
    assert "after switch second" not in catalog.log_path(first).read_text()
    inputs = iter(["/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    launch(project, "--session", "first session")
    assert catalog.latest_id() == first
    assert "已恢复此项目指定会话：first session" in capsys.readouterr().out
    assert len(catalog.entries()) == 2


def test_target_startup_failure_returns_to_saved_source(pair, monkeypatch, capsys):
    c, first, _ = pair
    project, second = c.store.project, c.store.id
    path = c.store.directory / f"{first}.json"
    bad = json.loads(path.read_text())
    bad["status"]["calls"] = -1  # Deep restoration validation, after store selection.
    path.write_text(json.dumps(bad))
    c.store.close()
    clients = mock_models(monkeypatch)
    inputs = iter(["/switch 1", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    launch(project)
    assert len(clients) == 3
    assert all(not client.requests for client in clients)
    assert SessionStore(project).catalog.latest_id() == second
    assert json.loads(path.read_text())["status"]["calls"] == -1
    assert "正在恢复原会话" in capsys.readouterr().out
    unlocked = SessionStore(project).open()
    unlocked.close()


def test_target_queue_restores_paused_without_running_any_task(pair, monkeypatch):
    c, first, _ = pair
    project, second = c.store.project, c.store.id
    source = c.store.data
    c.store.select(first)
    target = deepcopy(c.store.data)
    from agent.task_queue import TaskQueue

    queue = TaskQueue()
    queue.add("target queued work")
    target["task_queue"] = queue.snapshot()
    c.store.save(target)
    c.store.select(second)
    c.store.save(source)
    c.store.close()
    clients = mock_models(monkeypatch)
    seen = []

    def interactive(runtime, **kw):
        conv = kw["conversation"]
        seen.append(conv.store.id)
        if len(seen) == 1:
            return prepare_switch(conv, TaskController(runtime, conversation=conv), "/switch 1")
        assert conv.queue.paused
        assert conv.queue.pending[0]["text"] == "target queued work"
        assert runtime.execution_ledger is conv.ledger
        assert conv.ledger.store.id == first
        assert conv.archive.store.id == first

    monkeypatch.setattr(application, "run_interactive", interactive)
    launch(project)
    assert seen == [second, first]
    assert all(not client.requests for client in clients)


def test_runtime_and_environment_rebuilt_under_same_project_lock(pair, monkeypatch):
    c, first, _ = pair
    project, second = c.store.project, c.store.id
    c.store.close()
    clients = mock_models(monkeypatch)
    events = []

    def environment(args, root, store, capabilities, resources):
        with pytest.raises(ValueError, match="已有 Agent"):
            SessionStore(project).open()
        events.append(("open", store.id))
        resources.callback(events.append, ("close", store.id))
        return ExecutionEnvironment([])

    def interactive(runtime, **kw):
        conv = kw["conversation"]
        if conv.store.id == second:
            return prepare_switch(conv, TaskController(runtime, conversation=conv), "/switch 1")

    monkeypatch.setattr(application, "open_execution_environment", environment)
    monkeypatch.setattr(application, "run_interactive", interactive)
    launch(project)
    assert events == [("open", second), ("close", second), ("open", first), ("close", first)]
    assert len(clients) == 2


def test_teardown_failure_does_not_start_target(pair, monkeypatch):
    c, _, _ = pair
    project, second = c.store.project, c.store.id
    c.store.close()
    clients = mock_models(monkeypatch)

    def fail():
        raise OSError("cleanup failed")

    def environment(args, root, store, capabilities, resources):
        resources.callback(fail)
        return ExecutionEnvironment([])

    def interactive(runtime, **kw):
        conv = kw["conversation"]
        return prepare_switch(conv, TaskController(runtime, conversation=conv), "/switch 1")

    monkeypatch.setattr(application, "open_execution_environment", environment)
    monkeypatch.setattr(application, "run_interactive", interactive)
    with pytest.raises(SystemExit):
        launch(project)
    assert len(clients) == 1
    assert SessionStore(project).catalog.latest_id() == second


def test_tui_returns_handoff_and_saves_source_without_sending_model_request(pair):
    c, first, _ = pair

    async def scenario():
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                c.runtime,
                conversation=c,
                status=c.status,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(ui.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text('/switch "first session"\r')
            result = await asyncio.wait_for(task, 3)
            assert result == SessionSwitch(first)
            assert not ui.busy
            assert c.store.id != first
            assert any(
                "/switch" in b["text"]
                for b in c.store.data["transcript"]
                if isinstance(b.get("text"), str)
            )

    asyncio.run(scenario())


def test_continuation_uses_current_model_and_thinking_not_snapshot(pair):
    c, _, _ = pair
    original_args = SimpleNamespace(new_session=True, name="old", context_window=None)
    thinking = SimpleNamespace(
        current={"mode": "enabled", "effort": "low", "budget": None, "history": "on"},
        override={"efforts": ["low"]},
        base={},
    )
    session = SimpleNamespace(
        runtime=c.runtime, thinking=thinking, display=SimpleNamespace(mode="expanded")
    )
    args = continuation_args(original_args, session)
    assert args.model == c.runtime.llm.config.model and args.api_key == c.runtime.llm.config.api_key
    assert args.reasoning_effort == "low" and args.thinking_explicit
    assert args.thinking_display == "expanded"
    assert not args.new_session and args.name is None
    assert original_args.new_session and original_args.name == "old"


def test_docker_switch_resumes_each_original_copy_without_applying(tmp_path, monkeypatch):
    import shutil

    from cli import execution_environment
    from sandbox.environment import DockerEnvironment

    project = tmp_path / "project"
    project.mkdir()
    (project / "a").write_text("host original")
    mock_models(monkeypatch)
    monkeypatch.setattr(
        execution_environment,
        "detect_environment",
        lambda **kw: DockerEnvironment("standard", "test", "x86_64"),
    )
    monkeypatch.setattr(execution_environment, "check_image_profile", lambda *a, **kw: None)
    monkeypatch.setattr(
        "sandbox.session.DockerBackend", lambda policy: SimpleNamespace(healthy=True)
    )
    copies = []
    sessions = []

    def interactive(runtime, sandbox, conversation, **kwargs):
        step = len(copies)
        copies.append(sandbox.directory)
        sessions.append(conversation.store.id)
        if step in (0, 1):
            (sandbox.workspace / "a").write_text(f"copy {step}")
        elif step == 2:
            return prepare_switch(
                conversation,
                TaskController(runtime, conversation=conversation, sandbox=sandbox),
                "/switch 1",
            )
        elif step == 3:
            assert (sandbox.workspace / "a").read_text() == "copy 0"
            assert sandbox.guard.needs_review
            return prepare_switch(
                conversation,
                TaskController(runtime, conversation=conversation, sandbox=sandbox),
                "/switch 2",
            )
        else:
            assert (sandbox.workspace / "a").read_text() == "copy 1"

    monkeypatch.setattr(application, "run_interactive", interactive)
    try:
        for flags in ([], ["--new-session"], []):
            main.main(["--root", str(project), "--model", "m", "--sandbox", "docker", *flags])
        assert copies[0] != copies[1]
        assert copies == [copies[0], copies[1], copies[1], copies[0], copies[1]]
        assert sessions[3] == sessions[0] and sessions[4] == sessions[1]
        assert (project / "a").read_text() == "host original"
    finally:
        for path in set(copies):
            shutil.rmtree(path)


@pytest.mark.parametrize("selected_model", ["m", "test"])
def test_compressed_target_restores_summary_pins_archive_and_manual_window(
    pair, monkeypatch, selected_model
):
    from test_compaction import Model as SummaryModel
    from test_compaction import large_history

    from agent import AgentRuntime
    from agent.compaction import CompactionSettings
    from agent.conversation import SavedConversation
    from cli.session_status import SessionStatus

    c, first, _ = pair
    project, second = c.store.project, c.store.id
    c.store.select(first)
    model = SummaryModel()
    status = SessionStatus(project, context_window=24000)
    runtime = AgentRuntime(
        model, system_prompt="你是编码助手", max_output_tokens=1000, on_event=status
    )
    target = SavedConversation(
        c.store,
        runtime,
        model.config,
        status,
        compaction_settings=CompactionSettings(auto=False, keep_tokens=512),
    )
    status.context_command("/context 24000")
    target.checkpoint(history=large_history(), strict=True)
    target.compact()
    expected = json.loads(json.dumps(target.compaction_state))
    archived = target.archive.search("ORIGINAL_MARKER")["matches"][0]["reference"]
    target.store.select(second)
    target.store.save(target.store.data)
    target.store.close()
    clients = mock_models(monkeypatch)
    seen = []

    def interactive(runtime, conversation, **kw):
        seen.append(conversation.store.id)
        if len(seen) == 1:
            return prepare_switch(
                conversation, TaskController(runtime, conversation=conversation), "/switch 1"
            )
        assert conversation.compaction_state == expected
        assert conversation.status.context_override == (24000 if selected_model == "test" else None)
        assert conversation.archive.read(archived)["text"].startswith("ORIGINAL_MARKER")
        # Current model m differs from saved summary model test: native state is removed.
        if selected_model != "test":
            assert all(m.provider_state is None for m in conversation.history)
        assert conversation.history[2].content.startswith("[历史摘要；")

    monkeypatch.setattr(application, "run_interactive", interactive)
    launch(project, "--model", selected_model)
    assert seen == [second, first]
    assert all(not client.requests for client in clients)


def test_missing_archive_target_falls_back_without_overwriting_compacted_snapshot(
    pair, monkeypatch
):
    c, first, _ = pair
    project, second = c.store.project, c.store.id
    path = c.store.directory / f"{first}.json"
    data = json.loads(path.read_text())
    prefix = [Message("user", "历史交接"), Message("assistant", "历史摘要")]
    data["history"] = [m.to_dict() for m in prefix]
    data["compaction"] = {
        "snapshot": "a" * 32,
        "pins": [],
        "prefix": data["history"],
        "before": 100,
        "after": 50,
    }
    path.write_text(json.dumps(data))
    expected = path.read_bytes()
    c.store.close()
    mock_models(monkeypatch)
    inputs = iter(["/switch 1", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    launch(project)
    assert path.read_bytes() == expected
    assert SessionStore(project).catalog.latest_id() == second


def test_single_task_can_resume_selected_session(pair, monkeypatch):
    c, first, _ = pair
    project = c.store.project
    c.store.close()
    clients = mock_models(monkeypatch)
    launch(project, "--session", "1", "selected task")
    messages = [m.content for m in clients[0].requests[0].messages]
    assert "remember first" in messages and "remember second" not in messages
    assert "selected task" in messages
    assert SessionStore(project).catalog.latest_id() == first


@pytest.mark.parametrize("failure", ["environment", "checkpoint"])
def test_failed_target_setup_or_first_save_returns_to_source(pair, monkeypatch, failure):
    import subprocess

    c, first, _ = pair
    project, second = c.store.project, c.store.id
    c.store.close()
    mock_models(monkeypatch)
    original_open = application.open_execution_environment
    original_save = SessionStore.save

    def environment(args, root, store, capabilities, cleanup):
        if failure == "environment" and store.id == first:
            raise subprocess.CalledProcessError(1, "simulated sandbox setup")
        return original_open(args, root, store, capabilities, cleanup)

    def save(store, data):
        if failure == "checkpoint" and store.id == first:
            raise OSError("simulated target save failure")
        return original_save(store, data)

    monkeypatch.setattr(application, "open_execution_environment", environment)
    monkeypatch.setattr(SessionStore, "save", save)
    inputs = iter(["/switch 1", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    launch(project)
    assert SessionStore(project).catalog.latest_id() == second
