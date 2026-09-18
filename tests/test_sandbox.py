import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from sandbox import SandboxPolicy, SandboxSession
from sandbox.docker import DockerBackend
from tools.base import ToolResult


class Backend:
    def execute(self, workspace, name, arguments):
        return ToolResult(True, {"name": name, "root": str(workspace)})


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("before\n")
    value = SandboxSession(root, backend=Backend())
    yield value
    shutil.rmtree(value.directory)


def test_snapshot_filters_and_preserves_uncommitted(tmp_path, monkeypatch):
    (tmp_path / "source.py").write_text("uncommitted")
    for name in [".env", ".env.example", "private.pem", "credentials.txt"]:
        (tmp_path / name).write_text("secret")
    (tmp_path / "alias").symlink_to(tmp_path / "credentials.txt")
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "credentials.txt"))
    session = SandboxSession(tmp_path, backend=Backend())
    try:
        assert (session.workspace / "source.py").read_text() == "uncommitted"
        assert sorted(p.name for p in session.workspace.iterdir()) == [".git", "source.py"]
        assert session.changes()[1] == []
    finally:
        shutil.rmtree(session.directory)


def test_explicit_apply_modify_add_delete_and_reload(session):
    (session.workspace / "a.txt").write_text("after\n")
    (session.workspace / "b.txt").write_text("new")
    assert (session.root / "a.txt").read_text() == "before\n"
    assert "+after" in session.diff()
    restored = SandboxSession.review(session.directory)
    assert restored.apply() == ["a.txt", "b.txt"]
    assert restored.changes()[1] == []
    (restored.workspace / "a.txt").unlink()
    assert restored.apply() == ["a.txt"]
    assert not (session.root / "a.txt").exists()


def test_conflict_prevents_all_writes(session):
    (session.workspace / "a.txt").write_text("agent")
    (session.workspace / "b").write_text("agent")
    (session.root / "a.txt").write_text("human")
    with pytest.raises(ValueError, match="原项目"):
        session.apply()
    assert not (session.root / "b").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_export_rejected(session, kind):
    path = session.workspace / "unsafe"
    if kind == "symlink":
        path.symlink_to(session.root / "a.txt")
    elif kind == "hardlink":
        os.link(session.workspace / "a.txt", path)
    else:
        os.mkfifo(path)
    with pytest.raises(ValueError):
        session.apply()


def test_host_symlink_parent_cannot_redirect_write(session, tmp_path):
    (session.workspace / "new").mkdir()
    (session.workspace / "new" / "file").write_text("agent")
    (session.root / "new").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="符号链接"):
        session.apply()
    assert not (tmp_path / "file").exists()


def test_all_tools_are_proxies(session):
    for tool in session.tools():
        result = tool.execute({})
        assert result.data["root"] == str(session.workspace)
    assert {t.definition.name for t in session.tools()} >= {
        "run_python",
        "run_command",
        "git_diff",
        "git_status",
        "read_file",
        "write_file",
    }


def test_no_docker_fails_closed(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="不会退回"):
        DockerBackend(SandboxPolicy())


@pytest.mark.parametrize("interrupt", [False, True])
def test_container_security_and_cleanup(monkeypatch, tmp_path, interrupt):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="sha256:test\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    backend = DockerBackend(SandboxPolicy())

    def execute(command, **kwargs):
        calls.append(command)
        if interrupt:
            raise KeyboardInterrupt
        return SimpleNamespace(
            timed_out=False,
            exit_code=0,
            stdout_truncated=False,
            stdout=json.dumps({"success": True, "data": {}}),
        )

    monkeypatch.setattr(backend.runner, "run", execute)
    if interrupt:
        with pytest.raises(KeyboardInterrupt):
            backend.execute(tmp_path, "read_file", {"path": "a"})
    else:
        assert backend.execute(tmp_path, "read_file", {"path": "a"}).success
    command = calls[1]
    for flag in [
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit",
        "--memory",
    ]:
        assert flag in command
    assert calls[-1][1:3] == ["rm", "-f"]
    assert command[-1] == "sha256:test"


@pytest.mark.skipif(
    os.getenv("RUN_SANDBOX_DOCKER_TESTS") != "1",
    reason="Requires Docker and the built repo-agent-sandbox:v1 image",
)
def test_real_container_isolation(tmp_path):
    (tmp_path / ".env").write_text("never-visible")
    session = SandboxSession(tmp_path)
    try:
        tools = {tool.definition.name: tool for tool in session.tools()}
        assert tools["write_file"].execute({"path": "hello.txt", "content": "hello"}).success
        assert tools["read_file"].execute({"path": "hello.txt"}).success
        result = tools["run_python"].execute(
            {
                "code": (
                    "import os, socket\n"
                    "assert not os.path.exists('/workspace/.env')\n"
                    f"assert not os.path.exists({str(tmp_path)!r})\n"
                    "assert 'DEEPSEEK_API_KEY' not in os.environ\n"
                    "try:\n socket.create_connection(('1.1.1.1', 443), timeout=1)\n"
                    "except OSError:\n pass\n"
                    "else:\n raise AssertionError('network accessible')\n"
                )
            }
        )
        assert result.success and result.data["exit_code"] == 0
        assert tools["git_status"].execute({}).success
        assert not (tmp_path / "hello.txt").exists()
        session.apply()
        assert (tmp_path / "hello.txt").read_text() == "hello"
    finally:
        shutil.rmtree(session.directory)


def test_workspace_size_limit(tmp_path):
    from sandbox.session import files

    (tmp_path / "large").write_bytes(b"12345")
    with pytest.raises(ValueError, match="大小限制"):
        files(tmp_path, SandboxPolicy(max_workspace_bytes=4))


def test_protected_outputs_are_never_applied(session):
    (session.workspace / ".env").write_text("new key")
    (session.workspace / "logs").mkdir()
    (session.workspace / "logs" / "data").write_text("log")
    assert session.apply() == []
    assert not (session.root / ".env").exists()


def test_interactive_apply_is_user_command(session, monkeypatch, capsys):
    from cli.interactive import run_interactive

    (session.workspace / "a.txt").write_text("after")
    tasks = iter(["/diff", "/apply", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(tasks))
    run_interactive(None, sandbox=session)
    assert (session.root / "a.txt").read_text() == "after"
    assert "已回写 1" in capsys.readouterr().out


def test_review_cli_without_model(session, monkeypatch, capsys):
    from cli.main import main

    (session.workspace / "a.txt").write_text("after")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setattr("sys.argv", ["repo-agent", "--sandbox-review", str(session.directory)])
    main()
    assert (session.root / "a.txt").read_text() == "before\n"
    assert "+after" in capsys.readouterr().out
