"""Review permissions hold at registration, invocation, storage and session resume."""

import json
import os
from contextlib import ExitStack
from dataclasses import replace

import pytest
from session_helpers import Model

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from agent.task_reports import load_report, render_report
from cli import application, execution_environment, runtime_setup
from cli.arguments import parse_arguments, validate_execution_options
from cli.review_mode import guard_state_location, resolve_mode
from cli.session_status import SessionStatus
from cli.task_controller import TaskController
from host_support.git_worktree import Git
from llm import ToolCall
from tools import create_default_tools
from tools._internal.file_access import FileAccess
from tools.access_policy import AccessPolicy
from tools.dispatch import ToolDispatcher
from tools.filesystem import ReadFileTool, WriteFileTool
from tools.tool_groups import DEFAULT_TOOL_GROUPS


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.py").write_text("print('source')\n")
    return root


def test_runtime_exposes_only_audited_reads_and_cannot_reload_writers(project):
    runtime = AgentRuntime(
        Model(),
        create_default_tools(project, isolated_execution=True),
        access_policy=AccessPolicy("review"),
        tool_groups=DEFAULT_TOOL_GROUPS,
    )
    assert set(runtime._tools) == {
        "read_file",
        "list_files",
        "find_files",
        "search_files",
        "get_path_info",
        "load_tool_group",
    }
    assert "file_editing" in runtime.restore_tool_groups(["file_editing"])
    before = contents(project)
    read = runtime._execute(ToolCall("read", "read_file", {"reads": [{"path": "a.py"}]}))
    assert json.loads(read.content)["success"]
    response = runtime._execute(
        ToolCall("denied", "write_file", {"path": "a.py", "content": "bad"})
    )
    assert json.loads(response.content)["error"]["code"] == "UNKNOWN_TOOL"
    assert contents(project) == before


def test_dispatch_checks_exact_implementation_and_rechecks_at_call(project):
    dispatcher = ToolDispatcher(access_policy=AccessPolicy("review"))
    writer = WriteFileTool(project)
    with pytest.raises(ValueError, match="Review"):
        dispatcher.register(writer)

    class PretendReader(ReadFileTool):
        pass

    with pytest.raises(ValueError, match="Review"):
        dispatcher.register(PretendReader(project))
    # Even a faulty host extension inserting an unregistered tool cannot bypass dispatch.
    dispatcher.tools["write_file"] = writer
    assert dispatcher.execute("write_file", {}).error_code == "REVIEW_MODE_DENIED"
    git = next(
        t
        for t in create_default_tools(project, read_only=True)
        if t.definition.name == "git_status"
    )
    dispatcher.register(git)
    git.execution_allowed = True
    assert dispatcher.execute("git_status", {}).error_code == "REVIEW_MODE_DENIED"


@pytest.mark.parametrize("bound", [True, False])
def test_file_service_blocks_writes_even_without_dispatch(project, bound):
    writer = WriteFileTool(project)
    before = contents(project)
    with ExitStack() as cleanup:
        if bound:
            writer.read_only = True
        else:
            cleanup.enter_context(FileAccess(project, read_only_paths=(project,)).activate())
        result = writer.execute({"path": "a.py", "content": "bad", "overwrite": True})
    assert not result.success
    assert contents(project) == before


def test_review_git_reads_leave_metadata_unchanged_and_refuse_filters(project):
    git = Git(project)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("add", "a.py")
    git.run("commit", "-m", "initial")
    (project / "a.py").write_text("changed\n")
    tools = {t.definition.name: t for t in create_default_tools(project, read_only=True)}
    dispatcher = ToolDispatcher(access_policy=AccessPolicy("review"))
    for tool in tools.values():
        dispatcher.register(tool)
    before = contents(project)
    for name in ("git_status", "git_diff", "git_log", "git_show"):
        result = dispatcher.execute(name, {})
        assert result.success, result
    assert contents(project) == before
    git.run("config", "filter.untrusted.clean", "touch must-not-exist")
    (project / ".gitattributes").write_text("*.py filter=untrusted\n")
    assert not dispatcher.execute("git_status", {}).success
    assert not (project / "must-not-exist").exists()


def test_git_queries_do_not_launch_project_git(project, monkeypatch):
    binary = project / ("git.exe" if os.name == "nt" else "git")
    binary.write_text("#!/bin/sh\ntouch must-not-exist\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(project))
    tool = next(
        t
        for t in create_default_tools(project, read_only=True)
        if t.definition.name == "git_status"
    )
    assert not tool.execute({}).success
    assert not (project / "must-not-exist").exists()


@pytest.mark.parametrize("directory", [".", "nested repo"])
def test_review_git_show_handles_native_paths_and_keeps_path_checks(project, directory):
    from tools import GitShowTool

    root = project / directory
    root.mkdir(exist_ok=True)
    source = root / "src" / "space 中文.py"
    source.parent.mkdir()
    source.write_text("WINDOWS_PATCH_CONTENT\n", encoding="utf-8")
    (root / ".env").write_text("PRIVATE_HISTORY_CONTENT\n")
    git = Git(root)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("add", "-f", "--", "src/space 中文.py", ".env")
    git.run("commit", "-m", "nested paths")
    tool = GitShowTool(project)
    before = contents(project)

    result = tool.execute({"cwd": directory})
    assert result.success, result
    assert result.data["repo_root"] == directory
    assert "+WINDOWS_PATCH_CONTENT" in result.data["diff"]
    assert "PRIVATE_HISTORY_CONTENT" not in result.data["diff"]
    assert tool.execute({"cwd": directory, "path": "src\\space 中文.py"}).error_code == (
        "INVALID_ARGUMENTS"
    )
    assert contents(project) == before


def test_review_refuses_partial_clones_before_query(project):
    git = Git(project)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("config", "remote.origin.promisor", "true")
    before = contents(project)
    for tool in create_default_tools(project, read_only=True):
        if tool.definition.name.startswith("git_"):
            assert not tool.execute({}).success
    assert contents(project) == before


@pytest.mark.parametrize("backend", ["local", "native", "docker"])
def test_review_never_starts_command_backend(project, monkeypatch, backend):
    def forbidden(*args, **kwargs):
        pytest.fail("Review must not initialize a process backend")

    monkeypatch.setattr(execution_environment, "NativeBackend", forbidden)
    monkeypatch.setattr(execution_environment, "detect_environment", forbidden)
    _, args = parse_arguments(["--mode", "review", "--sandbox", backend])
    with ExitStack() as cleanup:
        env = execution_environment.open_execution_environment(args, project, None, None, cleanup)
    assert env.native is None and env.sandbox is None
    tools = {t.definition.name: t for t in env.tools}
    report = tools["get_execution_environment"].execute({}).data["execution"]
    assert report["writable_paths"] == []
    assert report["access_mode"] == "review"
    assert not report["command_execution_allowed"]
    assert (
        not {"run_command", "run_python", "run_shell", "write_file", "get_diagnostics"}
        & tools.keys()
    )


def test_queue_continue_and_restore_keep_review_permissions(project):
    class Reader(Model):
        def generate(self, request):
            result = super().generate(request)
            call = ToolCall(str(len(self.requests)), "read_file", {"reads": [{"path": "a.py"}]})
            return replace(
                result,
                message=replace(result.message, tool_calls=(call,), provider_state=None),
                finish_reason="tool_calls",
            )

    before = contents(project)
    model = Reader()
    with ExitStack() as cleanup:
        store = SessionStore(project).open()
        cleanup.callback(store.close)
        runtime = AgentRuntime(
            model,
            create_default_tools(project, read_only=True),
            access_policy=AccessPolicy("review"),
            max_steps=1,
        )
        conversation = SavedConversation(store, runtime, model.config, SessionStatus(project))
        controller = TaskController(runtime, conversation=conversation, write=lambda *a: None)
        controller.enqueue("review source")
        controller.resume()
        for index in range(2):
            if index:
                controller.continue_task()
            task = controller.start_next()
            assert task["attempts"][-1]["config"]["access_mode"] == "review"
            outcome = controller.execute(task)
            controller.finish(task, outcome)
        report = load_report(store, conversation.ledger)
        assert report["access_mode"] == "review"
        assert "只读审查" in render_report(report)
        sid = store.id
        assert store.data["access_mode"] == "review"
        conversation.new_session()
        assert store.id != sid and store.data["access_mode"] == "review"
        assert runtime.access_policy.read_only
    with ExitStack() as cleanup:
        restored = SessionStore(project, session=sid).open()
        cleanup.callback(restored.close)
        assert resolve_mode(restored, None) == "review"
        with pytest.raises(ValueError, match="new-session"):
            resolve_mode(restored, "develop")
        with pytest.raises(ValueError, match="权限模式"):
            SavedConversation(restored, AgentRuntime(model), model.config, SessionStatus(project))
    assert contents(project) == before


def test_application_resume_redirects_logs_and_skips_all_execution_setup(project, monkeypatch):
    model = Model()
    model.get_context_limit = lambda **kw: None
    monkeypatch.setattr(Model, "__enter__", lambda self: self, raising=False)
    monkeypatch.setattr(Model, "__exit__", lambda *a: None, raising=False)
    monkeypatch.setattr(runtime_setup, "LLMClient", lambda config: model)
    monkeypatch.setenv("AGENT_LOG_DIR", str(project / "logs"))

    def forbidden(*args, **kwargs):
        pytest.fail("Review must not initialize execution or Web tools")

    monkeypatch.setattr(execution_environment, "NativeBackend", forbidden)
    monkeypatch.setattr(application.WebBackend, "from_environment", forbidden)
    modes = []

    def interact(runtime, **options):
        c = options["conversation"]
        modes.append(c.access_mode)
        assert c.tracer.directory.is_relative_to(c.store.directory)
        assert not c.tracer.directory.is_relative_to(project)
        assert "只读审查" in c.notice

    monkeypatch.setattr(application, "run_interactive", interact)
    before = contents(project)
    for flags in (["--mode", "review"], []):
        parser, args = parse_arguments(["--root", str(project), "--model", "m", *flags])
        args.api_key = "SYNTHETIC_KEY"
        assert application.run_application(args, validate_execution_options(parser, args))
    assert modes == ["review", "review"]
    assert contents(project) == before


def test_review_state_inside_project_refused_before_creating_files(project, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(project / "state"))
    store = SessionStore(project)
    with pytest.raises(ValueError, match="XDG_STATE_HOME"):
        guard_state_location(store, "review")
    assert not (project / "state").exists()
    # Plain resume must not write even when permissions come from a persisted snapshot.
    store.directory.mkdir(parents=True)
    (store.directory / (store.id + ".json")).write_text(json.dumps({"access_mode": "review"}))
    before = contents(project)
    with pytest.raises(ValueError, match="XDG_STATE_HOME"):
        guard_state_location(store, None)
    assert contents(project) == before


@pytest.mark.parametrize(
    "flags", [["--workspace", "worktree"], ["--sandbox-writeback", "on-success"], ["--apply"]]
)
def test_review_rejects_mutating_lifecycle_options(flags):
    parser, args = parse_arguments(["--mode", "review", *flags])
    with pytest.raises(SystemExit):
        validate_execution_options(parser, args)
