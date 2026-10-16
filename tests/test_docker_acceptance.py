"""Acceptance harness contracts plus explicit real-engine opt-in.

Passing contracts on macOS/Linux is not Windows Desktop acceptance evidence.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support.cancellation import (
    CancellationContext,
    RunCancelled,
    cancellation_scope,
    current_cancellation,
)
from sandbox.docker_acceptance import Acceptance
from sandbox.worker import execute_request
from tools._internal.base import ToolResult
from tools.factory import create_file_tools


def worker_arguments(name, code, timeout=1):
    arguments = {"timeout_seconds": timeout}
    if name == "run_command":
        arguments["command"] = [sys.executable, "-u", "-c", code]
    elif name == "run_python":
        arguments["code"] = code
    else:
        arguments["script"] = "exec " + shlex.join([sys.executable, "-u", "-c", code])
    return arguments


@pytest.mark.skipif(os.name != "posix", reason="Worker runs inside a Linux container")
@pytest.mark.parametrize("name", ["run_command", "run_python", "run_shell"])
@pytest.mark.parametrize("mode", ["timeout", "cancel", "completed"])
def test_worker_cleans_detached_child_before_reporting(tmp_path, capsys, name, mode):
    """Exercise the real worker factory/runner path, not a configured test runner."""
    child = (
        "import os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('child.pid').write_text(str(os.getpid()))\n"
        "while True:\n Path('heartbeat').write_text(str(time.time_ns()))\n time.sleep(.02)\n"
    )
    # In the completed case, a child must be reclaimed even without inherited
    # output pipes keeping the collector alive. Other cases reproduce CI's pipes.
    redirect = (
        ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if mode == "completed" else ""
    )
    code = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True{redirect})\n"
        "while not Path('heartbeat').exists(): time.sleep(.01)\n"
        "print('ready', flush=True)\n"
        f"time.sleep({0.3 if mode == 'completed' else 30})\n"
    )
    context = CancellationContext()
    stop = threading.Event()

    def cancel():
        while not stop.wait(0.01):
            if (tmp_path / "heartbeat").exists():
                # Give the existing snapshot supervisor time to observe the child.
                if not stop.wait(0.15):
                    context.cancel()
                return

    watcher = threading.Thread(target=cancel, daemon=True)
    if mode == "cancel":
        watcher.start()
    verified = False
    try:
        request = {
            "name": name,
            "arguments": worker_arguments(name, code, 10 if mode == "cancel" else 1),
        }
        with cancellation_scope(context):
            if mode == "cancel":
                with pytest.raises(RunCancelled) as error:
                    execute_request(request, tmp_path)
                assert error.value.report["cleanup_status"] == "confirmed"
                assert capsys.readouterr().out == ""
            else:
                execute_request(request, tmp_path)
                result = json.loads(capsys.readouterr().out)
                data = result["data"]
                assert result["success"]
                assert data["timed_out"] == (mode == "timeout")
                assert data["output_complete"] == (mode == "completed")
                assert "ready" in data["stdout"]
                assert data["cleanup_status"] == "confirmed", data
                assert data["cleanup_error"] is None
                assert any(d["signal"] == signal.SIGKILL for d in data["cleanup_diagnostics"])
        heartbeat = tmp_path / "heartbeat"
        before = heartbeat.read_bytes()
        time.sleep(0.15)
        assert heartbeat.read_bytes() == before
        verified = True
    finally:
        stop.set()
        if mode == "cancel":
            watcher.join(timeout=2)
        # Bound failures in this regression: never leave our test child running.
        pid_file = tmp_path / "child.pid"
        if not verified and pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(os.name != "posix", reason="Worker runs inside a Linux container")
@pytest.mark.parametrize("name", ["run_command", "run_python", "run_shell"])
def test_worker_preserves_unknown_cleanup_when_process_inspection_fails(
    tmp_path, capsys, monkeypatch, name
):
    from host_support.supervision import ProcessTable

    def denied(self):
        raise PermissionError("Process snapshot denied")

    monkeypatch.setattr(ProcessTable, "snapshot", denied)
    execute_request(
        {"name": name, "arguments": worker_arguments(name, "import time; time.sleep(30)")},
        tmp_path,
    )
    result = json.loads(capsys.readouterr().out)
    data = result["data"]
    assert data["timed_out"] and not data["output_complete"]
    assert data["cleanup_status"] == "unknown"
    assert data["cleanup_error"]
    assert result["effects"]["status"] == "unknown"
    assert any(d["stage"] == "process_snapshot" for d in data["cleanup_diagnostics"])


@pytest.mark.parametrize("temporary_path", ["default", "alias"])
def test_writeback_acceptance_uses_real_host_files_without_engine(
    tmp_path, monkeypatch, temporary_path
):
    """Exercises Windows handles too when this contract runs on Windows CI."""
    from sandbox import session

    class FilesOnly:
        healthy = True
        image = "test-image"
        container_user = "test-user"
        runner = SimpleNamespace(run=lambda *a, **kw: None)

        def __init__(self, policy):
            pass

        def execute(self, workspace, name, arguments):
            tool = next(t for t in create_file_tools(workspace) if t.definition.name == name)
            return tool.execute(arguments)

    monkeypatch.setattr(session, "DockerBackend", FilesOnly)
    if temporary_path == "alias":
        import tempfile

        from host_support import filesystem

        target = tmp_path / "acceptance temporary directory"
        target.mkdir()
        if os.name == "nt":
            import ctypes as C

            short_path = C.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
            short_path.argtypes = [C.c_wchar_p, C.c_wchar_p, C.c_uint32]
            short_path.restype = C.c_uint32
            buffer = C.create_unicode_buffer(32768)
            size = short_path(str(target), buffer, len(buffer))
            assert 0 < size < len(buffer)
            alias = Path(buffer.value)
            if str(alias).casefold() == str(target).casefold():
                pytest.skip("This Windows volume does not create 8.3 aliases")
        else:
            alias = tmp_path / "temporary-alias"
            alias.symlink_to(target, target_is_directory=True)
        original = tempfile.mkdtemp
        created = []

        def mkdtemp(*args, **kwargs):
            if kwargs.get("prefix") == "repo-agent-acceptance-":
                kwargs["dir"] = alias
                path = original(*args, **kwargs)
                assert Path(path) != Path(path).resolve(strict=True)
                created.append(path)
                if os.name == "nt":
                    # Canonicalizing a trusted TEMP path must NOT relax the
                    # low-level service's rejection of short aliases.
                    with pytest.raises(PermissionError, match="short-name aliases"):
                        filesystem.open_directory(path)
                return path
            return original(*args, **kwargs)

        monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    acceptance = Acceptance()
    monkeypatch.setattr(acceptance, "assert_removed", lambda: None)
    acceptance.writeback()
    assert acceptance.report["retained_directories"] == []
    assert not acceptance.session.directory.exists()
    if temporary_path == "alias":
        assert len(created) == 1
        assert acceptance.session.directory.parent.parent == target.resolve(strict=True)


@pytest.mark.skipif(
    os.name == "nt" or not all(shutil.which(name) for name in ("bash", "node", "cc", "c++", "go")),
    reason="Requires a local POSIX C/C++/Go/Node toolchain; Docker CI covers the Linux image",
)
@pytest.mark.parametrize("compiler_failure", [False, True])
def test_compiler_acceptance_executes_and_removes_workspace_artifacts(tmp_path, compiler_failure):
    """Execute the real smoke script, including quoting and the EXIT cleanup."""
    workspace = tmp_path / "workspace 中文, with spaces"
    workspace.mkdir()
    # Leave an unrelated sentinel: only this call's build directory may be removed.
    (workspace / "sentinel").write_text("keep")
    acceptance = Acceptance()
    acceptance.called = set()
    calls = []
    environment = {
        **os.environ,
        "GOENV": "off",
        "GOTOOLCHAIN": "local",
        "GOWORK": "off",
        "GOPROXY": "off",
        "GOSUMDB": "off",
    }
    if compiler_failure:
        binaries = tmp_path / "broken-compiler"
        binaries.mkdir()
        compiler = binaries / "cc"
        compiler.write_text("#!/bin/sh\nexit 19\n")
        compiler.chmod(0o755)
        environment["PATH"] = str(binaries) + os.pathsep + os.environ["PATH"]

    def execute(arguments):
        completed = subprocess.run(
            [shutil.which("bash"), "-c", arguments["script"]],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=arguments["timeout_seconds"],
        )
        calls.append(completed)
        return ToolResult(True, {"exit_code": completed.returncode, "stderr": completed.stderr})

    acceptance.tools = {"run_shell": SimpleNamespace(execute=execute)}
    if compiler_failure:
        with pytest.raises(RuntimeError, match="exit_code.*19"):
            acceptance.compiled_tools()
    else:
        acceptance.compiled_tools()
    assert len(calls) == 1
    assert [p.name for p in workspace.iterdir()] == ["sentinel"]


@pytest.mark.parametrize("remaining,daemon_ok", [("repo-agent-owned\n", True), ("", False)])
def test_container_absence_requires_successful_daemon_query(monkeypatch, remaining, daemon_ok):
    acceptance = Acceptance()
    acceptance.session = SimpleNamespace(backend=SimpleNamespace(executable="docker"))
    acceptance.names = ["repo-agent-owned"]

    def query(command, **kwargs):
        assert command[1:3] == ["ps", "-a"] and kwargs["check"]
        if not daemon_ok:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout=remaining)

    monkeypatch.setattr(subprocess, "run", query)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        acceptance.assert_removed()


def test_cleanup_check_does_not_touch_unrelated_containers(monkeypatch):
    acceptance = Acceptance()
    acceptance.session = SimpleNamespace(backend=SimpleNamespace(executable="docker"))
    acceptance.names = ["repo-agent-owned"]
    calls = []

    def query(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="repo-agent-another-chat\n")

    monkeypatch.setattr(subprocess, "run", query)
    acceptance.assert_removed()
    assert len(calls) == 1 and "rm" not in calls[0]


def test_nonzero_command_cannot_pass_acceptance():
    acceptance = Acceptance()
    acceptance.called = set()
    acceptance.tools = {
        "run_command": SimpleNamespace(execute=lambda _: ToolResult(True, {"exit_code": 1}))
    }
    with pytest.raises(RuntimeError, match="exit_code"):
        acceptance.command("run_command", {"command": ["test"]})


def test_cancellation_records_host_uncertainty_without_erasing_it(tmp_path, monkeypatch):
    acceptance = Acceptance()

    @contextmanager
    def project():
        acceptance.names = ["repo-agent-owned"]
        acceptance.session = SimpleNamespace(workspace=tmp_path)

        def execute(_):
            context = current_cancellation()
            (tmp_path / "ready").write_text("ready")
            deadline = time.monotonic() + 3
            try:
                while time.monotonic() < deadline:
                    context.check()
                    time.sleep(0.01)
                pytest.fail("Cancellation never reached the running tool")
            except RunCancelled:
                context.record_cleanup("unknown", pid=123, error="host descendants unconfirmed")
                context.record_cleanup("confirmed", container="repo-agent-owned")
                raise

        acceptance.tools = {"run_command": SimpleNamespace(execute=execute)}
        yield

    monkeypatch.setattr(acceptance, "project", project)
    monkeypatch.setattr(acceptance, "stopped", lambda: None)
    with pytest.raises(RuntimeError, match="Host cleanup unconfirmed"):
        acceptance.cancellation()
    result = acceptance.report["cancellations"][0]
    assert result["method"] == "context_cancel"
    assert result["report"]["cleanup_status"] == "unknown"
    assert acceptance.report["manual_ctrl_c"] == "not_run"


def test_cli_writes_failure_report_instead_of_silently_skipping(tmp_path, monkeypatch):
    from sandbox import docker_acceptance

    report = tmp_path / "报告.json"
    monkeypatch.setattr(sys, "argv", ["acceptance", "--report", str(report)])

    def unavailable(**kwargs):
        raise ValueError("Docker unavailable")

    monkeypatch.setattr(docker_acceptance, "detect_environment", unavailable)
    with pytest.raises(SystemExit) as error:
        docker_acceptance.main()
    assert error.value.code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert not data["success"] and data["checks"] == []
    assert "Docker unavailable" in data["error"]


@pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_DOCKER_TESTS") != "1"
    and not (os.name == "nt" and os.environ.get("RUN_WINDOWS_DOCKER_TESTS") == "1"),
    reason="Requires a running Linux-container daemon and a freshly built standard image",
)
def test_real_docker_complete_acceptance(tmp_path):
    acceptance = Acceptance(os.environ.get("SANDBOX_LSP_TEST_IMAGE", "repo-agent-sandbox:v1"))
    try:
        acceptance.run()
    finally:
        path = Path(os.environ.get("DOCKER_ACCEPTANCE_REPORT", str(tmp_path / "acceptance.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(acceptance.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    assert acceptance.report["success"]
    assert len(acceptance.report["tools"]) == 25
    assert acceptance.report["manual_ctrl_c"] == "not_run"


@pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_DOCKER_TESTS") != "1"
    and not (os.name == "nt" and os.environ.get("RUN_WINDOWS_DOCKER_TESTS") == "1"),
    reason="Requires real Docker for scoped Python/npm imports and writeback",
)
def test_real_docker_scoped_dependencies(tmp_path, monkeypatch):
    import base64
    import hashlib

    from test_scoped_services import HTTP, approve, dependency, npm_archive, tool

    from tools.permissions import PermissionManager
    from tools.scoped_services import ScopedServices

    monkeypatch.setenv("REPO_AGENT_GITHUB_TOKEN", "docker-host-only-secret")
    acceptance = Acceptance(os.environ.get("SANDBOX_LSP_TEST_IMAGE", "repo-agent-sandbox:v1"))
    with acceptance.project():
        session = acceptance.session
        manager = PermissionManager(session.root, backend="docker")
        service = ScopedServices(
            manager, workspace=session.workspace, backend=session.backend, http=HTTP()
        )
        args = dependency(service)
        action = tool(service, "install_python_dependencies")
        approve(service, action.execute(args))
        installed = action.execute(args)
        assert installed.success, installed
        python_path = installed.data["directory"]
        blob = npm_archive()
        (session.workspace / "package-lock.json").write_text(
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "node_modules/demo": {
                            "resolved": "https://registry.npmjs.org/demo/-/demo-1.tgz",
                            "integrity": "sha512-"
                            + base64.b64encode(hashlib.sha512(blob).digest()).decode(),
                        }
                    },
                }
            )
        )
        service.http.calls.clear()
        service.http.respond = lambda *a, **kw: blob
        action = tool(service, "install_npm_dependencies")
        args = {"lock_path": "package-lock.json"}
        approve(service, action.execute(args))
        installed = action.execute(args)
        assert installed.success, installed
        node_path = installed.data["directory"]
        acceptance.command(
            "run_python",
            {
                "code": (
                    f"import sys,os; sys.path.insert(0, {python_path!r}); import demo; "
                    "assert demo.VALUE == 73; assert 'REPO_AGENT_GITHUB_TOKEN' not in os.environ"
                )
            },
        )
        acceptance.command(
            "run_command",
            {
                "command": [
                    "node",
                    "-e",
                    (
                        f"if (require('./{node_path}/node_modules/demo') !== 73 "
                        "|| process.env.REPO_AGENT_GITHUB_TOKEN) process.exit(1)"
                    ),
                ]
            },
        )
        assert not (session.root / python_path).exists()
        changed = session.apply()
        assert f"{python_path}/demo.py" in changed
        assert (session.root / python_path / "demo.py").is_file()
        assert not (session.root / node_path / "node_modules").exists()
        session.restore(session.last_backup.name)
        assert not (session.root / python_path / "demo.py").exists()
        assert not (session.root / "package-lock.json").exists()
        assert session.backend.healthy and not manager.profile.operations


@pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_DOCKER_TESTS") != "1"
    and not (os.name == "nt" and os.environ.get("RUN_WINDOWS_DOCKER_TESTS") == "1"),
    reason="Requires real Docker; simulated TUI input does not certify Windows console delivery",
)
def test_real_docker_tui_ctrl_c(tmp_path):
    """Actual UI cancellation path and actual container, with an offline fake model."""
    import asyncio

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent import AgentRuntime
    from cli.terminal.application import ConversationUI
    from llm import LLMResponse, Message, ToolCall
    from sandbox.environment import check_image_profile, detect_environment

    acceptance = Acceptance(os.environ.get("SANDBOX_LSP_TEST_IMAGE", "repo-agent-sandbox:v1"))

    class Model:
        calls = 0

        def generate(self, request):
            self.calls += 1
            message = (
                Message(
                    "assistant",
                    tool_calls=[
                        ToolCall(
                            "long",
                            "run_command",
                            {
                                "command": ["python", "-c", acceptance.long_command()],
                                "timeout_seconds": 100,
                            },
                        )
                    ],
                )
                if self.calls == 1
                else Message("assistant", "fresh task complete")
            )
            return LLMResponse(
                "test",
                "offline",
                message,
                finish_reason="tool_calls" if self.calls == 1 else "stop",
            )

    async def until(predicate, timeout=30):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait(), timeout)

    async def run():
        with acceptance.project():
            runtime = AgentRuntime(Model(), list(acceptance.tools.values()))
            with create_pipe_input() as pipe:
                ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
                task = asyncio.create_task(ui.run_async())
                try:
                    await until(lambda: ui.app.is_running)
                    pipe.send_text("start\r")
                    await until(lambda: (acceptance.session.workspace / "ready").exists())
                    first = ui.cancellation
                    pipe.send_text("\x03\x03")
                    await until(lambda: not ui.busy)
                    assert runtime.last_stats.status == "cancelled"
                    report = first.report()
                    acceptance.report["tui_cleanup"] = report
                    assert any(
                        x.get("container") in acceptance.names and x["status"] == "confirmed"
                        for x in report["cleanups"]
                    )
                    acceptance.assert_removed()
                    assert acceptance.session.guard.needs_review
                    assert not (acceptance.session.root / "heartbeat").exists()
                    before = (acceptance.session.workspace / "heartbeat").read_bytes()
                    await asyncio.sleep(0.3)
                    assert (acceptance.session.workspace / "heartbeat").read_bytes() == before
                    pipe.send_text("second\r")
                    await until(lambda: len(ui.controller.queue.pending) == 1)
                    assert ui.controller.queue.paused and runtime.llm.calls == 1
                    pipe.send_text("/queue resume\r")
                    assert report["cleanup_status"] == "confirmed", report
                    await until(lambda: "fresh task complete" in ui.transcript)
                    await until(lambda: not ui.busy)
                    assert ui.cancellation is not first
                    acceptance.report["tui_recovery"] = "resumed"
                    pipe.send_text("\x04")
                    await asyncio.wait_for(task, 10)
                finally:
                    if not task.done():
                        ui.cancelled.set()
                        ui.app.exit()
                        await asyncio.wait_for(task, 25)
        acceptance.report["success"] = True

    try:
        detect_environment(profile="standard", image=acceptance.image)
        check_image_profile(acceptance.image, "standard")
        asyncio.run(run())
    finally:
        path = Path(os.environ.get("DOCKER_TUI_REPORT", str(tmp_path / "tui.json")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(acceptance.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
