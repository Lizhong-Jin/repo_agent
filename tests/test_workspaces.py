"""Real Git lifecycle and integration contracts; no model service or network required."""

import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace

import pytest
from session_helpers import Model

from agent.session import SessionStore
from agent.task_reports import load_report
from agent.workspaces import Workspace, initialize_project, prepare_workspace
from cli import application, runtime_setup
from cli.arguments import parse_arguments, validate_execution_options
from cli.execution_environment import open_execution_environment
from cli.task_controller import TaskController
from cli.workspaces_command import main as workspace_command
from host_support.git_worktree import Git
from tools.filesystem import WriteFileTool


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path, monkeypatch):
    # Git intentionally discards inherited GIT_* variables in production. Apply
    # isolation to the test instances, after their environment is constructed.
    original = Git.__init__
    home = tmp_path / "git-home"
    home.mkdir()

    def initialize(self, root):
        original(self, root)
        self.env.update(
            HOME=str(home),
            USERPROFILE=str(home),
            XDG_CONFIG_HOME=str(home / ".config"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
        )

    monkeypatch.setattr(Git, "__init__", initialize)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "a.py").write_text("before\n", encoding="utf-8")
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8")
    git = Git(root)
    git.run("init", "--template=", "--initial-branch=main")
    git.run("add", "a.py", ".gitignore")
    git.run("commit", "-m", "baseline")
    return root


@pytest.fixture
def workspace(repository, tmp_path):
    with ExitStack() as cleanup:
        store = SessionStore(repository, directory=tmp_path / "state").open()
        cleanup.callback(store.close)
        workspace = prepare_workspace(store, "worktree")
        yield workspace


def test_init_preview_baseline_and_exclusions(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.py").write_text("hello\n")
    (root / ".env").write_text("secret")
    (root / ".gitignore").write_text("private-data/\n")
    for name in ("node_modules", "private-data"):
        (root / name).mkdir()
        (root / name / "large").write_text("not source")
    state = tmp_path / "state"
    preview = initialize_project(root, state)
    assert set(preview["files"]) == {"a.py", ".gitignore"}
    assert not (root / ".git").exists()
    result = initialize_project(root, state, confirm=True)
    assert result["state"] == "completed"
    assert set(Git(root).entries()) == {"a.py", ".gitignore"}
    Git(root).require_clean()
    assert (root / ".env").read_text() == "secret"
    with pytest.raises(ValueError, match="已属于"):
        initialize_project(root, state, confirm=True)


def test_init_refuses_parent_repository(repository, tmp_path):
    nested = repository / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="已属于"):
        initialize_project(nested, tmp_path / "state", confirm=True)
    assert not (nested / ".git").exists()


def test_dirty_source_refused_before_branch_creation(repository, tmp_path):
    (repository / "a.py").write_text("user changes")
    with ExitStack() as cleanup:
        store = SessionStore(repository, directory=tmp_path / "state").open()
        cleanup.callback(store.close)
        with pytest.raises(ValueError, match="未提交"):
            prepare_workspace(store, "worktree")
        assert not Workspace(store).path.exists()
        assert Git(repository).text("branch", "--list", "repo-agent/*") == ""


def test_review_merge_exact_content_and_preserve_source_until_acceptance(workspace):
    base = Git(workspace.project).head()
    tool = WriteFileTool(workspace.root)
    assert tool.execute({"path": "a.py", "content": "after\n", "overwrite": True}).success
    (workspace.root / "new binary").write_bytes(b"\x00\xff")
    (workspace.root / ".gitignore").unlink()
    review = workspace.review()
    assert "+after" in review["diff"] and "new binary" in review["diff"]
    assert (workspace.project / "a.py").read_text() == "before\n"
    assert Git(workspace.project).head() == base
    commit = workspace.merge(review["token"])
    assert Git(workspace.project).head() == commit
    assert (workspace.project / "a.py").read_text() == "after\n"
    assert (workspace.project / "new binary").read_bytes() == b"\x00\xff"
    assert not (workspace.project / ".gitignore").exists()
    assert Workspace(workspace.store).data["state"] == "merged"
    with pytest.raises(ValueError, match="merged"):
        prepare_workspace(workspace.store)


def test_review_invalidated_by_new_edit_and_execution(workspace):
    review = workspace.review()
    (workspace.root / "a.py").write_text("newer")
    with pytest.raises(ValueError, match="审查后"):
        workspace.merge(review["token"])
    workspace.begin_run("task")
    assert Workspace(workspace.store).data["state"] == "running"
    workspace.finish_run("confirmed")
    with pytest.raises(ValueError, match="先 workspaces review"):
        workspace.merge(review["token"])


@pytest.mark.parametrize("change", ["dirty", "advanced", "switched"])
def test_target_changes_never_overwritten(workspace, change):
    (workspace.root / "a.py").write_text("agent")
    review = workspace.review()
    git = Git(workspace.project)
    if change == "switched":
        git.run("checkout", "-b", "other")
    else:
        (workspace.project / "a.py").write_text("user")
        if change == "advanced":
            git.run("add", "a.py")
            git.run("commit", "-m", "user")
    head = git.head()
    with pytest.raises(ValueError):
        workspace.merge(review["token"])
    assert git.head() == head
    assert (workspace.project / "a.py").read_text() == (
        "before\n" if change == "switched" else "user"
    )


def test_restore_binding_and_missing_worktree_fail_closed(workspace):
    assert prepare_workspace(workspace.store).root == workspace.root
    with pytest.raises(ValueError, match="绑定"):
        prepare_workspace(workspace.store, "direct")
    moved = workspace.root.with_name(workspace.root.name + "-moved")
    workspace.root.rename(moved)
    with pytest.raises(ValueError, match="缺失"):
        prepare_workspace(workspace.store)


def test_unknown_cleanup_blocks_merge_and_requires_explicit_recovery(workspace):
    workspace.begin_run("task")
    interrupted = Workspace(workspace.store)
    with pytest.raises(ValueError, match="running"):
        interrupted.require_ready()
    with pytest.raises(ValueError, match="confirm-stopped"):
        interrupted.recover()
    interrupted.recover(confirm_stopped=True)
    interrupted.require_ready()
    interrupted.begin_run("another")
    interrupted.finish_run("unknown")
    with pytest.raises(ValueError, match="blocked"):
        interrupted.review()
    with pytest.raises(ValueError, match="blocked"):
        interrupted.discard()


def test_discard_archives_every_file_and_can_be_recovered(workspace):
    (workspace.root / "cache").mkdir()
    (workspace.root / "cache" / "ignored").write_bytes(b"keep")
    (workspace.root / "untracked").write_bytes(b"keep too")
    base = Git(workspace.project).head()
    workspace.discard()
    assert (workspace.root / "cache" / "ignored").read_bytes() == b"keep"
    assert (workspace.root / "untracked").read_bytes() == b"keep too"
    assert Git(workspace.project).head() == base
    restored = Workspace(workspace.store)
    restored.recover(confirm_stopped=True)
    restored.require_ready()


def test_creation_intent_survives_git_failure(repository, tmp_path, monkeypatch):
    store = SessionStore(repository, directory=tmp_path / "state").open()
    original = Git.run

    def fail(self, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            raise OSError("injected")
        return original(self, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Git, "run", fail)
            with pytest.raises(OSError):
                prepare_workspace(store, "worktree")
        restored = Workspace(store)
        assert restored.data["state"] == "creating"
        restored.recover()
        restored.require_ready()
    finally:
        store.close()


def test_merge_recovers_after_git_success_before_state_save(workspace, monkeypatch):
    (workspace.root / "a.py").write_text("after")
    review = workspace.review()
    save = workspace.save

    def fail():
        if workspace.data["state"] == "merged":
            raise OSError("injected")
        save()

    monkeypatch.setattr(workspace, "save", fail)
    with pytest.raises(OSError):
        workspace.merge(review["token"])
    restored = Workspace(workspace.store)
    assert restored.data["state"] == "merging"
    restored.recover()
    assert restored.data["state"] == "merged"
    assert (workspace.project / "a.py").read_text() == "after"


def test_host_operations_refuse_external_filters(workspace):
    git = Git(workspace.project)
    git.run("config", "filter.custom.clean", "must-not-execute")
    (workspace.root / ".gitattributes").write_text("a.py filter=custom\n")
    with pytest.raises(ValueError, match="过滤器"):
        workspace.review()


def test_local_worktree_keeps_local_permissions(workspace):
    _, args = parse_arguments(["--sandbox", "local"])
    with ExitStack() as cleanup:
        env = open_execution_environment(args, workspace.root, workspace.store, None, cleanup)
        tools = {t.definition.name: t for t in env.tools}
        assert "run_command" not in tools and "run_python" not in tools
        report = tools["get_execution_environment"].execute({}).data["execution"]
        assert report["workspace_kind"] == "worktree"
        assert report["workspace_root"] == str(workspace.root)
        assert report["command_execution_allowed"] is False
        assert tools["git_status"].execute({}).success


def test_queue_reports_and_history_use_same_worktree(repository, open_conversation):
    c = open_conversation(repository)
    workspace = prepare_workspace(c.store, "worktree")
    c.workspace, c.execution_root = workspace, workspace.root
    controller = TaskController(c.runtime, conversation=c, write=lambda *a: None)
    for text in ("first", "second"):
        controller.enqueue(text)
    controller.resume()
    for expected in ("first", "second"):
        task = controller.start_next()
        assert task["text"] == expected
        assert workspace.data["state"] == "running"
        outcome = controller.execute(task)
        controller.finish(task, outcome)
        assert workspace.data["state"] == "ready"
        report = load_report(c.store, c.ledger)
        assert report["workspace"] == str(workspace.root)
        assert report["project"] == str(repository)
        assert report["workspace_id"] == workspace.id
    assert c.store.data["workspace_id"] == workspace.id
    with pytest.raises(ValueError, match="绑定"):
        c.new_session()
    assert len(c.runtime.llm.requests) == 2


def test_application_restores_bound_workspace_without_flag(repository, monkeypatch):
    model = Model()
    model.get_context_limit = lambda **kw: None
    monkeypatch.setattr(Model, "__enter__", lambda self: self, raising=False)
    monkeypatch.setattr(Model, "__exit__", lambda *a: None, raising=False)
    monkeypatch.setattr(runtime_setup, "LLMClient", lambda config: model)
    roots = []
    skill_roots = []
    registry = runtime_setup.SkillRegistry

    def skills(root):
        skill_roots.append(root)
        return registry(root)

    monkeypatch.setattr(runtime_setup, "SkillRegistry", skills)

    def interact(runtime, **options):
        c = options["conversation"]
        roots.append(c.execution_root)
        assert c.workspace is not None
        assert c.execution_root != repository

    monkeypatch.setattr(application, "run_interactive", interact)
    for flags in (["--workspace", "worktree"], []):
        parser, args = parse_arguments(
            ["--root", str(repository), "--sandbox", "local", "--model", "m", *flags]
        )
        args.api_key = "SYNTHETIC_KEY"
        application.run_application(args, validate_execution_options(parser, args))
    assert roots[0] == roots[1]
    assert skill_roots == roots


def test_management_requires_project_lock_and_explicit_consent(workspace, capsys):
    # Default path uses the same key but this fixture overrides state location.
    from cli.workspaces_command import project_lock

    with project_lock(workspace.project):
        with pytest.raises(SystemExit) as caught:
            workspace_command(["list", "--root", str(workspace.project)])
    assert caught.value.code == 1
    assert "运行" in capsys.readouterr().err


def test_workspace_docker_rejected():
    parser, args = parse_arguments(["--sandbox", "docker", "--workspace", "worktree"])
    with pytest.raises(SystemExit):
        validate_execution_options(parser, args)


def test_ignored_original_file_is_not_overwritten(workspace):
    source = workspace.project
    (source / "cache").mkdir()
    (source / "cache" / "user-data").write_text("keep")
    (workspace.root / ".gitignore").write_text("")
    (workspace.root / "cache").mkdir()
    (workspace.root / "cache" / "user-data").write_text("agent")
    review = workspace.review()
    with pytest.raises(ValueError, match="Git merge"):
        workspace.merge(review["token"])
    assert (source / "cache" / "user-data").read_text() == "keep"
    Workspace(workspace.store).recover()


def test_assume_unchanged_cannot_hide_original_edits(workspace):
    git = Git(workspace.project)
    git.run("update-index", "--assume-unchanged", "a.py")
    (workspace.project / "a.py").write_text("hidden edit")
    with pytest.raises(ValueError, match="索引标记"):
        workspace.merge(workspace.review()["token"])


def test_init_recovery_after_initial_commit_publication(tmp_path, monkeypatch):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a").write_text("original")
    original = Git.run

    def fail(self, *args, **kwargs):
        if args[:2] == ("reset", "--mixed"):
            raise OSError("injected")
        return original(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Git, "run", fail)
        with pytest.raises(OSError):
            initialize_project(root, tmp_path / "state", confirm=True)
    head = Git(root).head()
    initialize_project(root, tmp_path / "state", confirm=True)
    assert Git(root).head() == head
    Git(root).require_clean()


def test_native_worktree_uses_its_root_and_host_git_readers(workspace, monkeypatch):
    from cli import execution_environment
    from tools import create_default_tools

    captured = {}

    class Native:
        def __init__(self, root, **options):
            captured.update(root=root, **options)

        def tools(self):
            return create_default_tools(captured["root"], isolated_execution=True)

        def execution_context(self):
            return {}

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(execution_environment, "NativeBackend", Native)
    _, args = parse_arguments(["--sandbox", "native", "--sandbox-profile", "standard"])
    with ExitStack() as cleanup:
        env = open_execution_environment(
            args, workspace.root, workspace.store, SimpleNamespace(label="test", gpu=False), cleanup
        )
        readers = {t.definition.name: t for t in env.tools}
        assert captured["isolated_workspace"] is True
        assert captured["root"] == workspace.root
        assert readers["git_status"].execution_allowed is False
        assert readers["git_status"].execute({}).success
        assert readers["write_file"].execute({"path": "b", "content": "worktree"}).success
        assert not (workspace.project / "b").exists()
    assert captured["closed"]


def test_continue_keeps_workspace_and_fresh_budget(repository, open_conversation):
    from test_continue import run_next
    from test_interactive import Model as SequenceModel
    from test_interactive import reply

    from llm import LLMConfig

    model = SequenceModel([reply("part", finish="length"), reply("done")])
    model.config = LLMConfig("deepseek", "m", api_key="test")
    c = open_conversation(repository, model=model)
    workspace = prepare_workspace(c.store, "worktree")
    c.workspace, c.execution_root = workspace, workspace.root
    c.runtime.max_steps = 1
    controller = TaskController(c.runtime, conversation=c, write=lambda *a: None)
    controller.enqueue("work")
    controller.resume()
    _, outcome = run_next(controller)
    assert outcome.value.status == "max_steps"
    (workspace.root / "a.py").write_text("partial result")
    controller.command("/continue")
    _, outcome = run_next(controller)
    assert outcome.value.status == "completed"
    assert (workspace.root / "a.py").read_text() == "partial result"
    assert (repository / "a.py").read_text() == "before\n"
    assert workspace.data["state"] == "ready"


def test_workspace_run_state_save_failure_prevents_model_execution(
    repository, open_conversation, monkeypatch
):
    c = open_conversation(repository)
    workspace = prepare_workspace(c.store, "worktree")
    c.workspace, c.execution_root = workspace, workspace.root
    controller = TaskController(c.runtime, conversation=c, write=lambda *a: None)
    controller.enqueue("work")
    controller.resume()

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(workspace, "save", fail)
    with pytest.raises(OSError):
        controller.start_next()
    assert not c.runtime.llm.requests
    assert controller.queue.paused


def test_management_review_and_merge_without_model_configuration(
    repository, open_conversation, capsys
):
    c = open_conversation(repository)
    workspace = prepare_workspace(c.store, "worktree")
    c.workspace, c.execution_root = workspace, workspace.root
    c.checkpoint(strict=True)
    (workspace.root / "a.py").write_text("accepted")
    c.store.close()
    options = ["--root", str(repository), "--session", workspace.id]
    workspace_command(["review", *options])
    token = capsys.readouterr().out.split("审查令牌：")[-1].strip()
    with pytest.raises(SystemExit):
        workspace_command(["merge", *options, "--review", token])
    assert (repository / "a.py").read_text() == "before\n"
    workspace_command(["merge", *options, "--review", token, "--yes"])
    assert (repository / "a.py").read_text() == "accepted"
    assert Workspace(c.store).data["state"] == "merged"


@pytest.mark.skipif(os.name == "nt", reason="Native Python environment selection is POSIX-only")
def test_native_worktree_does_not_inherit_launch_python(tmp_path, monkeypatch):
    from test_project_python import executable

    from sandbox.native_common import NativeBackendBase

    class Backend(NativeBackendBase):
        def _platform_setup(self):
            pass

        def _read_paths(self):
            return ()

        def _preflight(self):
            pass

        def _probe_project_python(self):
            return {}

    root = tmp_path / "workspace"
    root.mkdir()
    active = executable(tmp_path / "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(active.parent.parent))
    monkeypatch.setenv("AGENT_PROJECT_PYTHON", str(active))
    monkeypatch.setenv("PATH", str(active.parent))
    backend = Backend(root, profile="standard", isolated_workspace=True)
    try:
        assert backend.project_python.executable == type(root)(sys.executable).absolute()
        assert backend.project_python.source == "agent fallback"
    finally:
        backend.close()
    local = executable(root / ".venv")
    backend = Backend(root, profile="standard", isolated_workspace=True)
    try:
        assert backend.project_python.executable == local
    finally:
        backend.close()
    with pytest.raises(ValueError, match="必须位于"):
        Backend(
            root, profile="standard", isolated_workspace=True, project_python="../active/bin/python"
        )


def test_crlf_checkout_does_not_fabricate_changes(repository, tmp_path):
    git = Git(repository)
    (repository / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n")
    (repository / "lines.txt").write_bytes(b"line\n")
    git.run("add", ".gitattributes", "lines.txt")
    git.run("commit", "-m", "line endings")
    store = SessionStore(repository, directory=tmp_path / "state").open()
    try:
        workspace = prepare_workspace(store, "worktree")
        assert (workspace.root / "lines.txt").read_bytes() == b"line\r\n"
        assert workspace.review()["diff"] == ""
    finally:
        store.close()


def test_explicit_resume_after_workspace_created_before_first_snapshot(workspace):
    store = workspace.store
    assert store.data is None
    assert not (store.directory / "latest.json").exists()
    store.close()
    resumed = SessionStore(
        workspace.project, directory=store.directory.parent, session=workspace.id
    ).open()
    try:
        assert resumed.id == workspace.id
        assert prepare_workspace(resumed).root == workspace.root
        assert not (store.directory / "latest.json").exists()
    finally:
        resumed.close()


@pytest.mark.parametrize("restore_file", [False, True])
def test_protected_baseline_deletion_in_commit_cannot_bypass_review(
    repository, tmp_path, restore_file
):
    git = Git(repository)
    (repository / ".env").write_text("protected baseline")
    git.run("add", "-f", ".env")
    git.run("commit", "-m", "existing protected file")
    store = SessionStore(repository, directory=tmp_path / "state").open()
    try:
        workspace = prepare_workspace(store, "worktree")
        isolated = Git(workspace.root)
        isolated.run("rm", ".env")
        isolated.run("commit", "-m", "external removal")
        if restore_file:
            (workspace.root / ".env").write_text("changed protected file")
        with pytest.raises(ValueError, match="受保护"):
            workspace.review()
        assert (repository / ".env").read_text() == "protected baseline"
    finally:
        store.close()


@pytest.fixture
def global_git_config(tmp_path, monkeypatch):
    config = tmp_path / "global.gitconfig"
    config.write_text("")
    initialize = Git.__init__

    def use_test_global(self, root):
        initialize(self, root)
        self.env["GIT_CONFIG_GLOBAL"] = str(config)

    monkeypatch.setattr(Git, "__init__", use_test_global)
    return config


@pytest.mark.parametrize("location", ["global", "local"])
def test_unused_lfs_configuration_allows_full_workspace_lifecycle(
    tmp_path, global_git_config, location
):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("before\n")
    git = Git(root)
    if location == "local":
        initialize_project(root, tmp_path / "init-state", confirm=True)
    options = ["--file", str(global_git_config)] if location == "global" else ["--local"]
    for driver in ("clean", "smudge", "process"):
        git.run("config", *options, f"filter.lfs.{driver}", "must-not-execute")
    git.run("config", *options, "filter.lfs.required", "true")
    if location == "global":
        initialize_project(root, tmp_path / "init-state", confirm=True)
    store = SessionStore(root, directory=tmp_path / "state").open()
    try:
        workspace = prepare_workspace(store, "worktree")
        (workspace.root / "a.txt").write_text("after\n")
        review = workspace.review()
        assert "+after" in review["diff"]
        workspace.merge(review["token"])
        assert (root / "a.txt").read_text() == "after\n"
    finally:
        store.close()


@pytest.mark.parametrize("driver", ["clean", "smudge", "process"])
@pytest.mark.parametrize("attributes", ["worktree", "cached", "nested", "info", "global", "macro"])
def test_selected_filter_refused_without_execution(
    repository, tmp_path, global_git_config, driver, attributes
):
    from test_git_execution_policy import filter_command

    git = Git(repository)
    marker = tmp_path / "filter-executed"
    if attributes == "nested":
        directory = repository / "nested"
        directory.mkdir()
        (directory / "file with spaces.txt").write_text("content")
        (directory / ".gitattributes").write_text("*.txt filter=probe\n")
    elif attributes == "info":
        (repository / ".git/info").mkdir(exist_ok=True)
        (repository / ".git/info/attributes").write_text("a.py filter=probe\n")
    elif attributes == "global":
        path = tmp_path / "attributes"
        path.write_text("a.py filter=probe\n")
        git.run("config", "--file", str(global_git_config), "core.attributesFile", str(path))
    else:
        path = repository / ".gitattributes"
        path.write_text(
            "[attr]custom filter=probe\na.py custom\n"
            if attributes == "macro"
            else "a.py filter=probe\n"
        )
        if attributes == "cached":
            git.run("add", ".gitattributes")
            path.write_text("")  # The working copy cannot hide index attributes.
    git.run(
        "config", "--file", str(global_git_config), f"filter.probe.{driver}", filter_command(marker)
    )
    with pytest.raises(ValueError, match="过滤器"):
        git.check_filters()
    assert not marker.exists()


def test_destination_only_filter_blocks_checkout_and_can_recover(
    repository, tmp_path, global_git_config
):
    from test_git_execution_policy import filter_command

    git = Git(repository)
    (repository / ".gitattributes").write_text("a.py filter=probe\n")
    git.run("add", ".gitattributes")
    git.run("commit", "-m", "attributes without a configured driver")
    config = tmp_path / "worktree.gitconfig"
    marker = tmp_path / "filter-executed"
    git.run("config", "--file", str(config), "filter.probe.smudge", filter_command(marker))
    git.run(
        "config",
        "--file",
        str(global_git_config),
        "includeIf.onbranch:repo-agent/**.path",
        str(config),
    )
    store = SessionStore(repository, directory=tmp_path / "state").open()
    try:
        with pytest.raises(ValueError, match="过滤器"):
            prepare_workspace(store, "worktree")
        assert not marker.exists()
        workspace = Workspace(store)
        assert workspace.data["state"] == "creating"
        assert not (workspace.root / "a.py").exists()
        global_git_config.write_text("")
        workspace.recover()
        assert workspace.data["state"] == "ready"
        assert (workspace.root / "a.py").read_text() == "before\n"
    finally:
        store.close()


def test_merge_checks_new_paths_against_destination_attributes(
    repository, tmp_path, global_git_config
):
    from test_git_execution_policy import filter_command

    git = Git(repository)
    attributes = tmp_path / "source.attributes"
    attributes.write_text("new.txt filter=probe\n")
    config = tmp_path / "source.gitconfig"
    marker = tmp_path / "filter-executed"
    git.run("config", "--file", str(config), "core.attributesFile", str(attributes))
    git.run("config", "--file", str(config), "filter.probe.smudge", filter_command(marker))
    git.run("config", "--file", str(global_git_config), "includeIf.onbranch:main.path", str(config))
    store = SessionStore(repository, directory=tmp_path / "state").open()
    try:
        workspace = prepare_workspace(store, "worktree")
        (workspace.root / "new.txt").write_text("new file")
        review = workspace.review()
        head = git.head()
        index = (repository / ".git/index").read_bytes()
        with pytest.raises(ValueError, match="过滤器"):
            workspace.merge(review["token"])
        assert not marker.exists()
        assert not (repository / "new.txt").exists()
        assert git.head() == head
        assert (repository / ".git/index").read_bytes() == index
        assert workspace.data["state"] == "ready"
    finally:
        store.close()


@pytest.mark.parametrize("root_length", [240, 250])
def test_long_workspace_paths_support_checkout_queries_and_merge(
    repository, tmp_path_factory, root_length
):
    from tools.git_tools import GitDiffTool, GitLogTool, GitShowTool, GitStatusTool

    git = Git(repository)
    filename = "source_file_with_a_long_name.py"
    (repository / filename).write_text("before\n")
    git.run("add", "--", filename)
    git.run("commit", "-m", "long path fixture")
    git.run("config", "--local", "core.longpaths", "false")
    config = (repository / ".git/config").read_bytes()
    parent = tmp_path_factory.mktemp("long")
    # File paths may exceed MAX_PATH, but CreateProcessW still needs a cwd
    # of at most 258 UTF-16 units. Keep that independent OS limit explicit.
    prototype = parent / "workspaces" / ("0" * 64) / ("0" * 32)
    padding = root_length - len(str(prototype).encode("utf-16-le")) // 2 - 1
    assert padding > 0, "pytest temporary root is too long for the Windows boundary fixture"
    state = parent / ("s" * padding) / "sessions"
    store = SessionStore(repository, directory=state).open()
    try:
        workspace = prepare_workspace(store, "worktree")
        assert len(str(workspace.root).encode("utf-16-le")) // 2 == root_length
        target = workspace.root / filename
        assert len(str(target).encode("utf-16-le")) // 2 > 260
        assert target.read_text() == "before\n"
        target.write_text("after\n")
        for tool_type in (GitDiffTool, GitStatusTool, GitLogTool, GitShowTool):
            tool = tool_type(workspace.root)
            # Verify the effective subprocess setting on every host; the same
            # lifecycle test exercises real MAX_PATH handling in Windows CI.
            result = tool.runner.run(
                ["git", "config", "--get", "core.longpaths"], cwd=workspace.root
            )
            assert result.exit_code == 0 and result.stdout.strip() == "true"
            result = tool.execute({})
            assert result.success, result
            if tool_type in (GitDiffTool, GitStatusTool):
                assert filename in str(result.data)
        review = workspace.review()
        assert "+after" in review["diff"]
        workspace.merge(review["token"])
        assert (repository / filename).read_text() == "after\n"
        assert (repository / ".git/config").read_bytes() == config
    finally:
        store.close()


@pytest.mark.parametrize("operation", ["create", "recover_creation"])
def test_windows_overlong_workspace_rejected_before_git_changes(
    repository, tmp_path, monkeypatch, operation
):
    from host_support import git_worktree

    state = tmp_path / ("state-" + "x" * 60) / ("nested-" + "y" * 60) / "sessions"
    store = SessionStore(repository, directory=state).open()
    try:
        workspace = Workspace(store)
        assert len(str(workspace.root).encode("utf-16-le")) // 2 > 258
        git = Git(repository)
        if operation == "recover_creation":
            # Reproduce the previous release's interrupted no-checkout worktree.
            workspace.data = {
                **workspace.identity(),
                "state": "creating",
                "base": git.head(),
                "target": git.branch(),
                "common": str(git.check_repository()),
            }
            workspace.save()
            workspace.root.parent.mkdir(parents=True, exist_ok=True)
            git.run(
                "worktree",
                "add",
                "--no-checkout",
                "-b",
                workspace.branch.removeprefix("refs/heads/"),
                workspace.root,
                git.head(),
            )
            (workspace.root / "keep.txt").write_text("user data")
            saved = workspace.path.read_bytes()
            pointer = (workspace.root / ".git").read_bytes()
        before = git.run("show-ref"), git.run("worktree", "list", "--porcelain")
        monkeypatch.setattr(git_worktree, "sys", SimpleNamespace(platform="win32"))
        with pytest.raises(ValueError, match="XDG_STATE_HOME"):
            if operation == "create":
                prepare_workspace(store, "worktree")
            else:
                Workspace(store).recover()
        if operation == "create":
            assert not workspace.root.exists()
            assert not workspace.path.exists()
        else:
            assert workspace.path.read_bytes() == saved
            assert (workspace.root / ".git").read_bytes() == pointer
            assert (workspace.root / "keep.txt").read_text() == "user data"
        assert before == (git.run("show-ref"), git.run("worktree", "list", "--porcelain"))
    finally:
        store.close()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_git_directory_limit_counts_utf16_units(monkeypatch, platform):
    from host_support import git_worktree

    monkeypatch.setattr(git_worktree, "sys", SimpleNamespace(platform=platform))
    boundary = "C:\\" + "x" * 253 + "😀"
    git_worktree.check_git_directory(boundary)
    if platform == "win32":
        with pytest.raises(ValueError, match="258"):
            git_worktree.check_git_directory(boundary + "x")
    else:
        git_worktree.check_git_directory(boundary + "x" * 100)


def test_windows_overlong_git_root_rejected_before_executable_lookup(tmp_path, monkeypatch):
    from host_support import git_worktree

    root = tmp_path / ("x" * 100) / ("y" * 100)
    root.mkdir(parents=True)
    monkeypatch.setattr(git_worktree, "sys", SimpleNamespace(platform="win32"))

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("overlong cwd must be rejected before preparing a Git process")

    monkeypatch.setattr(git_worktree, "find_windows_executable", unexpected_lookup)
    monkeypatch.setattr(git_worktree.shutil, "which", unexpected_lookup)
    with pytest.raises(ValueError, match="258"):
        Git(root)
