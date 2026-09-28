"""Cross-process conversation continuity, recovery and project isolation."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from agent.transcript import Transcript
from cli.session_status import SessionStatus
from cli.terminal.application import ConversationUI
from llm import LLMClient, LLMConfig, LLMResponse, Message, ToolCall, Usage
from llm.schemas import ProviderState
from sandbox import SandboxPolicy, SandboxSession
from tools._internal.file_policy import session_state_root
from tools.filesystem import ReadFileTool


class Model:
    def __init__(self, config=None):
        self.config = config or LLMConfig("deepseek", "m", api_key="SYNTHETIC_KEY")
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        message = Message("assistant", "记住了：蓝色")
        message = replace(
            message,
            provider_state=ProviderState(
                self.config.provider,
                self.config.model,
                {"content": message.content, "reasoning_content": "visible thought"},
                message.fingerprint(),
            ),
        )
        return LLMResponse(
            provider=self.config.provider,
            model=self.config.model,
            message=message,
            finish_reason="stop",
            usage=Usage(100, 10),
        )


@pytest.fixture
def open_conversation(tmp_path):
    stores = []

    def create(project=None, *, new=False, model=None):
        project = project or tmp_path / "project"
        project.mkdir(exist_ok=True)
        store = SessionStore(project, new=new).open()
        stores.append(store)
        model = model or Model()
        status = SessionStatus(project, context_window=1000)
        runtime = AgentRuntime(model, on_event=status)
        conversation = SavedConversation(store, runtime, model.config, status)
        conversation.checkpoint(strict=True)
        return conversation

    yield create
    for store in stores:
        store.close()


def perform(conversation, text):
    conversation.start_task(text)
    result = conversation.runtime.run(text, history=conversation.history)
    conversation.finish_task(result)
    return result


def read_saved(project):
    directory = SessionStore(project).directory
    pointer = json.loads((directory / "latest.json").read_text())
    return json.loads((directory / (pointer["session_id"] + ".json")).read_text())


def test_startup_estimate_is_visible_saved_and_recomputed_on_resume(open_conversation):
    first = open_conversation()
    assert first.status.context_tokens > 0
    assert "本地粗估" in first.status.describe_context(compact=True)
    assert not first.runtime.llm.requests
    assert first.status.calls == 0 and not any(first.status.totals.values())
    # Simulate a stale estimate from an earlier client version.
    first.status.context_tokens = first.status.context_input_tokens = 1
    first.checkpoint(strict=True)
    first.store.close()
    second = open_conversation()
    assert second.status.context_tokens > 1
    assert "本地粗估" in second.status.describe_context(compact=True)
    assert not second.runtime.llm.requests
    assert read_saved(second.store.project)["status"]["calls"] == 0


def test_restore_native_messages_transcript_usage_and_context(open_conversation):
    first = open_conversation()
    perform(first, "记住蓝色")
    sid, history = first.store.id, first.history
    first.store.close()
    second = open_conversation()
    assert second.store.id == sid and second.history == history
    assert second.history[-1].provider_state.payload["reasoning_content"] == "visible thought"
    assert second.status.totals == {"input_tokens": 100, "output_tokens": 10}
    assert second.status.calls == 1 and second.status.context_tokens == 110
    assert "记住蓝色" in second.transcript.render("collapsed")[0]
    perform(second, "是什么颜色？")
    assert second.runtime.llm.requests[0].messages[:-1] == history
    assert second.status.calls == 2 and second.status.totals["input_tokens"] == 200
    assert second.runtime._task_number == 2


def test_tool_pairs_restore_without_execution(open_conversation):
    first = open_conversation()
    call = ToolCall("read-once", "read_file", {"reads": [{"path": "a.txt"}]})
    history = (
        Message("user", "读取"),
        Message("assistant", tool_calls=(call,)),
        Message.tool_result(call, {"text": "blue"}),
    )
    first.checkpoint(history=history, strict=True)
    first.store.close()
    second = open_conversation()
    assert second.runtime.llm.requests == []
    perform(second, "根据刚才的结果回答")
    messages = second.runtime.llm.requests[0].messages
    assert any(m.tool_call_id == "read-once" for m in messages)
    assert second.runtime.last_stats.tool_calls == []


def test_new_session_preserves_old_snapshot_and_is_default_next_time(open_conversation):
    first = open_conversation()
    perform(first, "old")
    old_id, directory = first.store.id, first.store.directory
    first.store.close()
    fresh = open_conversation(new=True)
    assert fresh.store.id != old_id and fresh.history == ()
    assert fresh.status.calls == 0
    assert json.loads((directory / f"{old_id}.json").read_text())["history"]
    new_id = fresh.store.id
    fresh.store.close()
    resumed = open_conversation()
    assert resumed.store.id == new_id and not resumed.history


def test_project_paths_are_isolated_and_symlink_alias_resumes(open_conversation, tmp_path):
    first = open_conversation(tmp_path / "first")
    perform(first, "only first")
    first.store.close()
    other = open_conversation(tmp_path / "second")
    assert not other.history
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "first", target_is_directory=True)
    resumed = open_conversation(alias)
    assert any(m.content == "only first" for m in resumed.history)


def test_pending_task_restores_safe_history_and_uncertainty_without_replay(open_conversation):
    first = open_conversation()
    perform(first, "completed")
    first.start_task("run expensive edit")
    first.store.close()  # Simulate process loss after intent was saved.
    second = open_conversation()
    assert "上次任务未完整结束" in second.history[-1].content
    assert "run expensive edit" in second.history[-1].content
    assert any(m.content == "completed" for m in second.history)
    assert not second.runtime.llm.requests
    assert second.pending_task is None and second.status.context_tokens > 0
    assert "本地粗估" in second.status.describe_context()
    second.store.close()
    again = open_conversation()
    assert sum("上次任务未完整结束" in m.content for m in again.history) == 1


def test_clear_survives_restart_but_keeps_session_totals(open_conversation):
    first = open_conversation()
    perform(first, "old")
    first.clear()
    first.store.close()
    second = open_conversation()
    assert not second.history and second.status.calls == 1
    assert second.status.context_tokens > 0
    assert "本地粗估" in second.status.describe_context()


def test_new_command_resets_totals_and_retains_files(open_conversation):
    conversation = open_conversation()
    path = conversation.store.project / "result.txt"
    path.write_text("keep")
    perform(conversation, "old")
    old_id = conversation.store.id
    conversation.new_session()
    assert conversation.store.id != old_id and not conversation.history
    assert conversation.status.calls == 0 and path.read_text() == "keep"


def test_model_change_removes_native_state_and_preserves_text(open_conversation):
    first = open_conversation()
    perform(first, "remember")
    first.store.close()
    second = open_conversation(model=Model(LLMConfig("qwen", "other", api_key="key")))
    assert any(m.content == "remember" for m in second.history)
    assert all(m.provider_state is None for m in second.history)
    assert second.status.context_tokens > 0 and second.status.calls == 1
    assert "本地粗估" in second.status.describe_context()
    perform(second, "continue")


def test_changed_system_prompt_takes_effect_without_editing_assistant_state(open_conversation):
    first = open_conversation()
    perform(first, "remember")
    first.store.close()
    store = SessionStore(first.store.project).open()
    try:
        model = Model()
        runtime = AgentRuntime(model, system_prompt="new system")
        second = SavedConversation(store, runtime, model.config, SessionStatus(store.project))
        assert second.history[0] == Message("system", "new system")
        assert second.history[-1] == first.history[-1]
        assert second.status.context_tokens > 0
        assert "本地粗估" in second.status.describe_context()
    finally:
        store.close()


def test_corruption_does_not_overwrite_last_record_and_new_bypasses_it(open_conversation):
    first = open_conversation()
    path = first.store.directory / "latest.json"
    first.store.close()
    path.write_text("not json")
    with pytest.raises(ValueError, match="--new-session"):
        open_conversation()
    assert path.read_text() == "not json"
    fresh = open_conversation(new=True)
    assert not fresh.history


def test_incomplete_tool_record_is_rejected(open_conversation):
    first = open_conversation()
    path = first.store.directory / f"{first.store.id}.json"
    data = json.loads(path.read_text())
    data["history"] = [
        Message("user", "x").to_dict(),
        Message("assistant", tool_calls=(ToolCall("x", "write_file", {}),)).to_dict(),
    ]
    path.write_text(json.dumps(data))
    first.store.close()
    with pytest.raises(ValueError, match="--new-session"):
        open_conversation()


def test_permissions_lock_and_credentials_not_serialized(open_conversation):
    first = open_conversation()
    with pytest.raises(ValueError, match="已有 Agent 会话"):
        open_conversation()
    assert first.store.directory.stat().st_mode & 0o777 == 0o700
    for path in first.store.directory.glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert "SYNTHETIC_KEY" not in path.read_text()
    first.store.close()
    assert open_conversation().store.id == first.store.id


def test_atomic_write_failure_retains_prior_snapshot(open_conversation, monkeypatch):
    first = open_conversation()
    perform(first, "old")
    path = first.store.directory / f"{first.store.id}.json"
    before = path.read_bytes()
    monkeypatch.setattr(
        "agent.session.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="尚未执行新任务"):
        first.start_task("new")
    assert path.read_bytes() == before
    assert not list(first.store.directory.glob(".session-*"))
    assert len(first.runtime.llm.requests) == 1


def test_failed_new_command_retains_active_session(open_conversation, monkeypatch):
    first = open_conversation()
    perform(first, "old")
    sid, history = first.store.id, first.history
    save = first.store.save

    def fail_new(record):
        if first.store.id != sid:
            raise OSError("disk full")
        return save(record)

    monkeypatch.setattr(first.store, "save", fail_new)
    with pytest.raises(OSError, match="当前会话保留"):
        first.new_session()
    assert first.store.id == sid and first.history == history
    assert first.status.calls == 1


def test_cli_auto_resume_and_new_flag(tmp_path, monkeypatch, capsys):
    from cli import main as cli

    calls = []

    def factory(config):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "answer"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                },
            )

        client = LLMClient(config, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        client._owned = True
        return client

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr("cli.runtime_setup.LLMClient", factory)
    for task, flags in [("remember blue", []), ("what color", []), ("fresh", ["--new-session"])]:
        monkeypatch.setattr(
            "sys.argv",
            ["repo-agent", "--sandbox", "local", "--model", "m", "--root", str(project), *flags],
        )
        inputs = iter([task, "/exit"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        cli.main()
    assert any(m.get("content") == "remember blue" for m in calls[1]["messages"])
    assert not any(m.get("content") == "remember blue" for m in calls[2]["messages"])
    assert "已恢复此项目上次会话" in capsys.readouterr().out
    assert read_saved(project)["status"]["calls"] == 1
    assert len(list(SessionStore(project).directory.glob("*.json"))) == 3


def test_single_task_cli_resumes(tmp_path, monkeypatch):
    from cli import main as cli

    models = []

    def factory(config):
        model = Model(config)
        model.get_context_limit = lambda **kw: None
        model.close = lambda: None
        models.append(model)
        return ContextClient(model)

    class ContextClient:
        def __init__(self, model):
            self.model = model

        def __enter__(self):
            return self.model

        def __exit__(self, *args):
            pass

    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr("cli.runtime_setup.LLMClient", factory)
    for task in ("first", "second"):
        monkeypatch.setattr(
            "sys.argv",
            ["repo-agent", task, "--sandbox", "local", "--model", "m", "--root", str(tmp_path)],
        )
        cli.main()
    assert any(m.content == "first" for m in models[1].requests[0].messages)
    assert read_saved(tmp_path)["status"]["calls"] == 2


def test_tui_restore_visible_history_and_exit_persists_new_task(open_conversation):
    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 3)

    async def run():
        first = open_conversation()
        perform(first, "old message")
        first.store.close()
        second = open_conversation()
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                second.runtime,
                status=second.status,
                conversation=second,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            assert "old message" in ui.transcript
            running = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("new message\r")
            await until(lambda: not ui.busy and len(second.runtime.llm.requests) == 1)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(running, 3)
        second.store.close()
        third = open_conversation()
        assert any(m.content == "new message" for m in third.history)
        assert third.status.calls == 2

    asyncio.run(run())


def test_transcript_roundtrip_keeps_thinking_and_escapes_controls():
    transcript = Transcript()
    transcript.append("你> hi\n", kind="user")
    transcript.thinking("thinking_start", "", 1, 0.1)
    transcript.thinking("thinking_delta", "thought\x1b[2J", 1, 0.2)
    transcript.thinking("thinking_end", "", 1, 0.3)
    restored = Transcript.from_records(transcript.to_records())
    assert restored.render("expanded") == transcript.render("expanded")
    assert "\x1b" not in restored.render("expanded")[0]


def test_sandbox_resume_preserves_copy_and_requires_review(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a").write_text("original")
    backend = SimpleNamespace(healthy=True)
    first = SandboxSession(root, backend=backend)
    try:
        (first.workspace / "a").write_text("unpublished")
        resumed = SandboxSession.resume(first.directory, root, SandboxPolicy(), backend=backend)
        assert resumed.workspace == first.workspace
        assert (resumed.workspace / "a").read_text() == "unpublished"
        assert (root / "a").read_text() == "original"
        assert resumed.guard.needs_review
        resumed.begin_task()
        assert resumed.guard.needs_review
        assert resumed.baseline == first.baseline
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(ValueError, match="--new-session"):
            SandboxSession.resume(first.directory, other, SandboxPolicy(), backend=backend)
    finally:
        import shutil

        shutil.rmtree(first.directory)


def test_missing_saved_sandbox_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match="--new-session"):
        SandboxSession.resume(
            tmp_path / "missing", tmp_path, SandboxPolicy(), backend=SimpleNamespace()
        )


def test_state_files_excluded_from_tools_and_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    store = SessionStore(tmp_path).open()
    try:
        path = store.directory / "private.json"
        path.write_text("conversation contents")
        relative = path.relative_to(tmp_path).as_posix()
        assert ReadFileTool(tmp_path).execute({"reads": [{"path": relative}]}).data["results"][0]["error"]["code"] == "PROTECTED_FILE"
        sandbox = SandboxSession(tmp_path, backend=SimpleNamespace())
        try:
            assert not (sandbox.workspace / session_state_root().relative_to(tmp_path)).exists()
        finally:
            import shutil

            shutil.rmtree(sandbox.directory)
    finally:
        store.close()


def test_separate_cli_processes_resume_from_disk(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    source = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    capture = tmp_path / "request.json"
    script = """
import sys
from pathlib import Path
from cli import main as cli
from llm import LLMClient
import httpx

project, capture, task = sys.argv[1:]
capture_path = Path(capture)
def factory(config):
    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        capture_path.write_bytes(request.content)
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "saved answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    client = LLMClient(config, http_client=httpx.Client(transport=httpx.MockTransport(reply)))
    client._owned = True
    return client
from cli import runtime_setup
runtime_setup.LLMClient = factory
sys.argv = ["repo-agent", task, "--sandbox", "local", "--model", "m", "--root", project]
cli.main()
"""
    env = {**os.environ, "PYTHONPATH": str(source), "DEEPSEEK_API_KEY": "synthetic-key"}
    for task in ("first process", "second process"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(project), str(capture), task],
            env=env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    request = json.loads(capture.read_text())
    assert any(m.get("content") == "first process" for m in request["messages"])
    assert any(m.get("content") == "saved answer" for m in request["messages"])
    assert read_saved(project)["status"]["calls"] == 2


def test_cli_reconnects_original_sandbox_and_new_flag_creates_copy(tmp_path, monkeypatch):
    import shutil
    from contextlib import nullcontext

    from cli import main as cli
    from sandbox.environment import DockerEnvironment

    project = tmp_path / "project"
    project.mkdir()
    (project / "a").write_text("original")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    monkeypatch.setattr("cli.runtime_setup.LLMClient", lambda config: nullcontext(Model(config)))
    monkeypatch.setattr(
        "cli.execution_environment.detect_environment", lambda **kw: DockerEnvironment("standard", "test", "x86_64")
    )
    monkeypatch.setattr("cli.execution_environment.check_image_profile", lambda *args, **kw: None)
    monkeypatch.setattr(
        "sandbox.session.DockerBackend", lambda policy: SimpleNamespace(healthy=True)
    )
    copies = []

    def interactive(runtime, *, sandbox, conversation, **kwargs):
        copies.append(sandbox.directory)
        if len(copies) == 1:
            (sandbox.workspace / "a").write_text("unpublished")
            perform(conversation, "first sandbox task")
        elif len(copies) == 2:
            assert copies[1] == copies[0]
            assert (sandbox.workspace / "a").read_text() == "unpublished"
            assert sandbox.guard.needs_review
            assert any(m.content == "first sandbox task" for m in conversation.history)
        else:
            assert copies[2] != copies[0]
            assert (sandbox.workspace / "a").read_text() == "original"
            assert not conversation.history

    monkeypatch.setattr("cli.application.run_interactive", interactive)
    try:
        for flags in ([], [], ["--new-session"]):
            monkeypatch.setattr(
                "sys.argv", ["repo-agent", "--sandbox", "docker", "--model", "m", "--root", str(project), *flags]
            )
            cli.main()
    finally:
        for directory in set(copies):
            shutil.rmtree(directory)


@pytest.mark.parametrize("command, expected_calls", [("/clear", 2), ("/new", 1)])
def test_tui_clear_and_new_survive_restart(open_conversation, command, expected_calls):
    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 3)

    async def run():
        conversation = open_conversation()
        perform(conversation, "old context")
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                conversation.runtime,
                status=conversation.status,
                conversation=conversation,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text(command + "\r")
            await until(lambda: not ui.history)
            pipe.send_text("fresh context\r")
            await until(lambda: not ui.busy and len(conversation.runtime.llm.requests) == 2)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)
        conversation.store.close()
        restored = open_conversation()
        assert not any(m.content == "old context" for m in restored.history)
        assert any(m.content == "fresh context" for m in restored.history)
        assert restored.status.calls == expected_calls
        assert ("old context" in restored.transcript.render("collapsed")[0]) == (
            command == "/clear"
        )

    asyncio.run(run())


def test_context_override_restores_without_reusing_old_configured_window(tmp_path):
    configured = SessionStatus(tmp_path, context_window=1000)
    auto = SessionStatus(tmp_path)
    auto.restore_session(configured.session_state())
    assert auto.context_window is None
    configured.context_command("/context 900")
    auto.restore_session(configured.session_state())
    assert auto.context_window == 900
    explicit = SessionStatus(tmp_path, context_window=500)
    explicit.restore_session(configured.session_state(), restore_window=False)
    assert explicit.context_window == 500
