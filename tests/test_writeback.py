import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.Tracing import RunStats
from cli.interactive import finish_writeback, run_interactive
from sandbox import SandboxSession
from tools._internal.base import ToolResult


class Backend:
    healthy = True

    def execute(self, workspace, name, arguments):
        return self.result


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a").write_text("before")
    instance = SandboxSession(root, backend=Backend())
    (instance.workspace / "a").write_text("after")
    yield instance
    shutil.rmtree(instance.directory)


@pytest.fixture
def standard_cli_environment(monkeypatch):
    """Writeback unit tests use a fake backend, including Docker preflight."""
    from cli import main as cli
    from sandbox.environment import DockerEnvironment

    monkeypatch.setattr("llm.LLMClient.get_context_limit", lambda self, **kw: None)

    monkeypatch.setattr(
        "cli.execution_environment.detect_environment", lambda **kw: DockerEnvironment("standard", "test", "x86_64")
    )
    monkeypatch.setattr("cli.execution_environment.check_image_profile", lambda *a, **kw: None)


def completed(status="completed"):
    return SimpleNamespace(status=status, stats=RunStats(1))


def record(session, code=0, command="pytest", **flags):
    tool = next(t for t in session.tools() if t.definition.name == "run_command")
    session.backend.result = ToolResult(True, {"exit_code": code, **flags})
    return tool.execute({"command": [command]})


def test_manual_default_does_not_apply(session):
    assert finish_writeback(session, completed(), "manual")
    assert (session.root / "a").read_text() == "before"


def test_success_applies_once_and_keeps_original_backup(session, capsys):
    record(session)
    assert finish_writeback(session, completed(), "on-success")
    backup = session.last_backup
    assert (session.root / "a").read_text() == "after"
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert (backup / manifest["files"]["a"]["blob"]).read_text() == "before"
    assert finish_writeback(session, completed(), "on-success")
    assert len(list((session.directory / "backups").iterdir())) == 1
    assert "已自动回写 1" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [{"timed_out": True}, {"cleanup_error": "not cleaned"}])
def test_timeout_and_cleanup_block_even_zero_exit(session, flags):
    record(session, **flags)
    assert not finish_writeback(session, completed(), "on-success")
    assert (session.root / "a").read_text() == "before"


def test_unrelated_success_cannot_hide_failed_test_but_same_retry_can(session):
    record(session, 1)
    record(session, 0, "echo")
    assert not finish_writeback(session, completed(), "on-success")
    record(session, 0)
    assert finish_writeback(session, completed(), "on-success")


@pytest.mark.parametrize("status", ["max_steps", "stopped"])
def test_incomplete_task_is_not_applied_by_next_unrelated_task(session, status):
    assert not finish_writeback(session, completed(status), "on-success")
    assert not finish_writeback(session, completed(), "on-success")
    assert (session.root / "a").read_text() == "before"
    session.apply()  # Explicit review acknowledges the pending changes.
    assert not session.guard.needs_review


def test_conflict_blocks_whole_batch_before_backup(session):
    (session.workspace / "b").write_text("new")
    (session.root / "a").write_text("human")
    assert not finish_writeback(session, completed(), "on-success")
    assert not (session.root / "b").exists()
    assert not (session.directory / "backups").exists()


def test_backup_restores_add_modify_delete_and_preserves_later_user_edits(session):
    (session.root / "a").chmod(0o600)
    (session.workspace / "a").unlink()
    (session.workspace / "new").write_text("created")
    session.apply()
    backup = session.last_backup.name
    (session.root / "new").write_text("human")
    with pytest.raises(ValueError, match="拒绝恢复"):
        session.restore(backup)
    assert not (session.root / "a").exists()
    (session.root / "new").write_text("created")
    restored = SandboxSession.review(session.directory)
    assert restored.restore(backup) == ["a", "new"]
    assert (session.root / "a").read_text() == "before"
    assert (session.root / "a").stat().st_mode & 0o777 == 0o600
    assert not (session.root / "new").exists()
    assert restored.restore(backup) == []


def test_partial_write_failure_has_recoverable_backup(session, monkeypatch):
    import sandbox.session as module

    (session.workspace / "b").write_text("new")
    replace = module.os.replace

    def fail_second(source, target, **kwargs):
        if target == "b":
            raise OSError("injected disk failure")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(module.os, "replace", fail_second)
    assert not finish_writeback(session, completed(), "on-success")
    assert (session.root / "a").read_text() == "after"
    assert not (session.root / "b").exists()
    backup_id = session.last_backup.name
    monkeypatch.setattr(module.os, "replace", replace)
    assert session.restore(backup_id) == ["a"]
    assert (session.root / "a").read_text() == "before"


def test_backup_failure_never_changes_original(session, monkeypatch):
    import sandbox.writeback as module

    monkeypatch.setattr(module, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        session.apply()
    assert (session.root / "a").read_text() == "before"


def test_interrupted_task_does_not_leak_into_later_completed_task(session, monkeypatch):
    tasks = iter(["change", "hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(tasks))

    class Runtime:
        count = 0

        def run(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                raise KeyboardInterrupt
            return SimpleNamespace(
                status="completed", stats=RunStats(2), text="hi", undisplayed_text="hi", history=()
            )

    run_interactive(Runtime(), sandbox=session, writeback="on-success")
    assert (session.root / "a").read_text() == "before"


def test_user_template_preserves_writeback_setting_on_reinstall(tmp_path, monkeypatch):
    from installer.setup import configure_user

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path))
    config = configure_user(root)
    assert config.read_bytes() == (root / ".env.example").read_bytes()
    config.write_text("AGENT_SANDBOX_WRITEBACK=on-success\n# user's settings\n")
    before = config.read_bytes()
    configure_user(root)
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "env,flags,expected",
    [
        ("on-success", [], "on-success"),
        ("on-success", ["--sandbox-writeback", "manual"], "manual"),
        ("", [], "manual"),
    ],
)
def test_cli_config_precedence(
    session, monkeypatch, env, flags, expected, standard_cli_environment
):
    from cli import main as cli

    monkeypatch.setenv("AGENT_SANDBOX_WRITEBACK", env)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock")
    monkeypatch.setattr("sys.argv", ["repo-agent", "--sandbox", "docker", "--model", "test", *flags])
    monkeypatch.setattr("cli.execution_environment.SandboxSession", lambda *a, **kw: session)
    seen = []
    monkeypatch.setattr("cli.application.run_interactive", lambda *a, **kw: seen.append(kw["writeback"]))
    cli.main()
    assert seen == [expected]


def test_invalid_env_mode_fails_before_docker(monkeypatch):
    from cli.main import main

    monkeypatch.setenv("AGENT_SANDBOX_WRITEBACK", "invalid")
    monkeypatch.setattr("sys.argv", ["repo-agent", "--sandbox", "docker"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.skipif(
    os.getenv("RUN_SANDBOX_DOCKER_TESTS") != "1",
    reason="Requires Docker and the sandbox image",
)
def test_real_docker_failure_repair_auto_apply_and_restore(tmp_path):
    sandbox = SandboxSession(tmp_path)
    try:
        tools = {t.definition.name: t for t in sandbox.tools()}
        assert tools["write_file"].execute({"path": "value.txt", "content": "bad"}).success
        args = {
            "command": [
                "python",
                "-c",
                "from pathlib import Path; assert Path('value.txt').read_text() == 'ok'",
            ]
        }
        assert tools["run_command"].execute(args).data["exit_code"] != 0
        assert not finish_writeback(sandbox, completed(), "on-success")
        assert not (tmp_path / "value.txt").exists()
        assert (
            tools["write_file"]
            .execute({"path": "value.txt", "content": "ok", "overwrite": True})
            .success
        )
        assert tools["run_command"].execute(args).data["exit_code"] == 0
        assert finish_writeback(sandbox, completed(), "on-success")
        assert (tmp_path / "value.txt").read_text() == "ok"
        sandbox.restore(sandbox.last_backup.name)
        assert not (tmp_path / "value.txt").exists()
    finally:
        shutil.rmtree(sandbox.directory)


@pytest.mark.parametrize("failed", [False, True])
def test_single_task_cli_applies_after_run_and_signals_failure(
    session, monkeypatch, failed, standard_cli_environment
):
    from contextlib import nullcontext

    from cli import main as cli

    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent",
            "task",
            "--sandbox",
            "docker",
            "--model",
            "test",
            "--root",
            str(session.root),
            "--sandbox-writeback",
            "on-success",
        ],
    )
    monkeypatch.setattr("cli.execution_environment.SandboxSession", lambda *a, **kw: session)
    monkeypatch.setattr("cli.runtime_setup.LLMClient", lambda *a: nullcontext(object()))

    def run(task, *, history=()):
        assert not history
        assert task == "task"
        assert (session.root / "a").read_text() == "before"
        record(session, 1 if failed else 0)
        return SimpleNamespace(
            status="completed", stats=RunStats(1), text="done", undisplayed_text="done",
            history=(), response=SimpleNamespace(text="done"),
        )

    monkeypatch.setattr("cli.runtime_setup.AgentRuntime", lambda *a, **kw: SimpleNamespace(
        run=run, llm=a[0], _task_number=0, estimate_context_tokens=lambda history=(): 0,
        restore_tool_groups=lambda names: (), loaded_tool_groups=(),
        reset_tool_groups=lambda: None,
    ))
    if failed:
        with pytest.raises(SystemExit) as error:
            cli.main()
        assert error.value.code == 1
    else:
        cli.main()
    assert (session.root / "a").read_text() == ("before" if failed else "after")


def test_interactive_applies_between_tasks(session, monkeypatch):
    tasks = iter(["one", "two", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(tasks))

    class Runtime:
        count = 0

        def run(self, task, **kwargs):
            self.count += 1
            if self.count == 2:
                assert (session.root / "a").read_text() == "after"
                (session.workspace / "a").write_text("second")
            return SimpleNamespace(
                status="completed",
                stats=RunStats(self.count),
                text="done",
                undisplayed_text="done",
                history=(),
            )

    run_interactive(Runtime(), sandbox=session, writeback="on-success")
    assert (session.root / "a").read_text() == "second"
    assert len(list((session.directory / "backups").iterdir())) == 2


def test_read_only_negative_test_does_not_block_next_edit(session, capsys):
    (session.workspace / "a").write_text("before")
    record(session, 2, "negative-input-test")
    assert finish_writeback(session, completed(), "on-success")
    assert "无需自动回写" in capsys.readouterr().out
    assert not session.guard.pending
    (session.workspace / "a").write_text("new feature")
    record(session, 0)
    assert finish_writeback(session, completed(), "on-success")
    assert (session.root / "a").read_text() == "new feature"


def test_clean_task_boundary_resets_old_failure_but_dirty_boundary_retains_it(session):
    record(session, 2)
    session.begin_task()
    assert session.guard.pending  # Fixture already has unpublished changes.
    (session.workspace / "a").write_text("before")
    session.guard.needs_review = True
    session.begin_task()
    assert not session.guard.pending and not session.guard.needs_review


def test_no_changes_does_not_override_unhealthy_container(session):
    (session.workspace / "a").write_text("before")
    session.backend.healthy = False
    assert not finish_writeback(session, completed(), "on-success")


def test_failed_read_only_task_does_not_poison_next_interactive_edit(session, monkeypatch):
    from llm import LLMTimeoutError

    (session.workspace / "a").write_text("before")
    tasks = iter(["inspect", "edit", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(tasks))

    class Runtime:
        def run(self, task, **kwargs):
            if task == "inspect":
                record(session, 2)
                raise LLMTimeoutError("timeout")
            (session.workspace / "a").write_text("edited")
            return SimpleNamespace(
                status="completed",
                stats=RunStats(2),
                text="done",
                undisplayed_text="done",
                history=(),
            )

    run_interactive(Runtime(), sandbox=session, writeback="on-success")
    assert (session.root / "a").read_text() == "edited"


def test_failed_command_with_changes_reports_exit_code(session, capsys):
    record(session, 2)
    assert not finish_writeback(session, completed(), "on-success")
    assert "退出码 2" in capsys.readouterr().out
    assert (session.root / "a").read_text() == "before"


@pytest.mark.skipif(
    os.getenv("RUN_SANDBOX_DOCKER_TESTS") != "1",
    reason="Requires Docker and the sandbox image",
)
def test_real_docker_read_only_negative_check_then_edit(tmp_path, capsys):
    (tmp_path / "app.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('size', type=int)\na = p.parse_args()\n"
        "if a.size < 1: p.error('size must be positive')\n"
    )
    sandbox = SandboxSession(tmp_path)
    try:
        tools = {t.definition.name: t for t in sandbox.tools()}
        negative = tools["run_command"].execute({"command": ["python", "app.py", "0"]})
        assert negative.data["exit_code"] == 2
        assert finish_writeback(sandbox, completed(), "on-success")
        assert "无需自动回写" in capsys.readouterr().out
        sandbox.begin_task()
        assert tools["write_file"].execute({"path": "README.md", "content": "usage"}).success
        # A negative test is successful only when its assertion checks the expected status.
        checked = tools["run_python"].execute(
            {
                "code": (
                    "import subprocess, sys\n"
                    "r = subprocess.run([sys.executable, 'app.py', '0'], capture_output=True)\n"
                    "assert r.returncode == 2\n"
                )
            }
        )
        assert checked.data["exit_code"] == 0
        assert finish_writeback(sandbox, completed(), "on-success")
        assert (tmp_path / "README.md").read_text() == "usage"
    finally:
        shutil.rmtree(sandbox.directory)


def python_check(session, code, exit_code, check_id=None, cwd="."):
    session.backend.result = ToolResult(True, {"exit_code": exit_code})
    arguments = {"code": code, "cwd": cwd}
    if check_id is not None:
        arguments["check_id"] = check_id
    return next(t for t in session.tools() if t.definition.name == "run_python").execute(arguments)


def test_corrected_named_python_check_can_resolve_failure(session):
    python_check(session, "assert False", 1, "maze")
    python_check(session, "assert True", 0, "other")
    assert not finish_writeback(session, completed(), "on-success")
    python_check(session, "assert True", 0, "maze")
    assert finish_writeback(session, completed(), "on-success")


def test_check_id_does_not_cross_tool_or_directory(session):
    python_check(session, "assert False", 1, "maze")
    python_check(session, "assert True", 0, "maze", "tests")
    session.backend.result = ToolResult(True, {"exit_code": 0})
    next(t for t in session.tools() if t.definition.name == "run_command").execute(
        {"command": ["true"], "check_id": "maze"}
    )
    assert not finish_writeback(session, completed(), "on-success")


def test_unnamed_python_retries_still_require_exact_code(session):
    python_check(session, "assert False", 1)
    python_check(session, "assert True", 0)
    assert not finish_writeback(session, completed(), "on-success")


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, None, 1, []])
def test_invalid_check_id_never_executes_backend(session, bad):
    tool = next(t for t in session.tools() if t.definition.name == "run_python")
    assert not tool.execute({"code": "pass", "check_id": bad}).success


def test_final_verification_failure_then_retry(session, capsys):
    session.verify_command = ["python", "-m", "unittest"]
    session.backend.result = ToolResult(True, {"exit_code": 1, "stderr": "assertion failed"})
    assert not finish_writeback(session, completed(), "on-success")
    assert (session.root / "a").read_text() == "before"
    report = json.loads((session.directory / "verification.json").read_text())
    assert report["data"]["stderr"] == "assertion failed"
    session.backend.result = ToolResult(True, {"exit_code": 0})
    assert finish_writeback(session, completed(), "on-success")


@pytest.mark.parametrize("status", ["max_steps", "completed"])
def test_final_verification_cannot_override_existing_blocks(session, status):
    session.verify_command = ["true"]
    if status == "completed":
        python_check(session, "assert False", 1, "maze")
    # Backend would raise AttributeError if invoked: final check must not run here.
    if hasattr(session.backend, "result"):
        del session.backend.result
    assert not finish_writeback(session, completed(status), "on-success")


def test_final_verification_cleanup_failure_requires_manual_review(session):
    session.verify_command = ["true"]
    session.backend.result = ToolResult(True, {"exit_code": 0, "cleanup_error": "unknown"})
    assert not finish_writeback(session, completed(), "on-success")
    session.backend.result = ToolResult(True, {"exit_code": 0})
    assert not finish_writeback(session, completed(), "on-success")


@pytest.mark.skipif(os.getenv("RUN_SANDBOX_DOCKER_TESTS") != "1", reason="Requires Docker")
def test_real_docker_named_retry_and_final_verification(tmp_path):
    (tmp_path / "a").write_text("before")
    sandbox = SandboxSession(
        tmp_path,
        verify_command=[
            "python",
            "-c",
            "from pathlib import Path; assert Path('a').read_text() == 'after'",
        ],
    )
    try:
        (sandbox.workspace / "a").write_text("after")
        tool = next(t for t in sandbox.tools() if t.definition.name == "run_python")
        assert tool.execute({"code": "assert False", "check_id": "maze"}).data["exit_code"] == 1
        assert tool.execute({"code": "assert True", "check_id": "maze"}).data["exit_code"] == 0
        assert finish_writeback(sandbox, completed(), "on-success")
        assert (tmp_path / "a").read_text() == "after"
    finally:
        shutil.rmtree(sandbox.directory)


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], ["python", "check.py"]),
        (["--sandbox-verify-command", '["python", "other.py"]'], ["python", "other.py"]),
        (["--sandbox-verify-command", ""], None),
    ],
)
def test_cli_verification_config_precedence(
    session, monkeypatch, flags, expected, standard_cli_environment
):
    from cli import main as cli

    monkeypatch.setenv("AGENT_SANDBOX_VERIFY_COMMAND", '["python", "check.py"]')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock")
    monkeypatch.setattr("sys.argv", ["repo-agent", "--sandbox", "docker", "--model", "test", *flags])
    seen = []

    def construct(*args, **kwargs):
        seen.append(kwargs["verify_command"])
        return session

    monkeypatch.setattr("cli.execution_environment.SandboxSession", construct)
    monkeypatch.setattr("cli.application.run_interactive", lambda *a, **kw: None)
    cli.main()
    assert seen == [expected]
