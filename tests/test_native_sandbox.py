"""Native policy failures must never become unrestricted host execution."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_sessions import open_conversation  # noqa: F401

from sandbox.native import NativeBackend, seatbelt_profile
from tools.process_runner import ProcessResult


def test_native_rejects_other_platforms_before_creating_state(monkeypatch, tmp_path):
    monkeypatch.setattr("sandbox.native.sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="仅支持 macOS"):
        NativeBackend(tmp_path)


def test_policy_escapes_paths_and_does_not_grant_network_or_hardlinks(tmp_path):
    root = tmp_path / 'project "quoted" (name)'
    profile = seatbelt_profile(root, tmp_path / "scratch", [tmp_path / "runtime"], [])
    assert '(deny default)' in profile
    assert '(deny network*)' in profile and '(allow network' not in profile
    assert '(deny file-link)' in profile
    assert json.dumps(str(root)) in profile
    with pytest.raises(ValueError, match="控制字符"):
        seatbelt_profile(Path("/tmp/bad\npath"), tmp_path, [], [])


@pytest.mark.parametrize("flags,mode", [([], "native"), (["--sandbox", "native"], "native"),
                                      (["--sandbox", "local"], "local")])
def test_cli_defaults_to_native_despite_old_writeback_config(tmp_path, monkeypatch, flags, mode):
    from cli import main as cli
    from llm import LLMClient

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("AGENT_SANDBOX_WRITEBACK", "on-success")
    monkeypatch.setattr(sys, "argv", ["repo-agent", "--root", str(tmp_path), "--model", "m", *flags])
    monkeypatch.setattr(LLMClient, "get_context_limit", lambda *a, **kw: None)
    native_calls = []
    closed = []

    def native_backend(workspace):
        native_calls.append(workspace)
        return SimpleNamespace(healthy=True, tools=lambda: [], close=lambda: closed.append(True))

    monkeypatch.setattr(cli, "NativeBackend", native_backend)
    monkeypatch.setattr(cli, "detect_environment", lambda **kw: pytest.fail("Docker selected"))
    observed = []

    def interactive(runtime, **kwargs):
        observed.append(kwargs["conversation"].mode)
        assert kwargs.get("sandbox") is None

    monkeypatch.setattr(cli, "run_interactive", interactive)
    cli.main()
    assert observed == [mode]
    assert native_calls == ([tmp_path] if mode == "native" else [])
    assert closed == ([True] if mode == "native" else [])


def test_explicit_direct_auto_writeback_is_rejected(monkeypatch, tmp_path):
    from cli.main import main

    for mode in ("local", "native"):
        monkeypatch.setattr(sys, "argv", ["repo-agent", "--root", str(tmp_path),
                                          "--sandbox", mode, "--sandbox-writeback", "on-success"])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 2


def test_native_session_roundtrip(open_conversation):
    conversation = open_conversation()
    conversation.mode = "native"
    conversation.checkpoint(strict=True)
    project = conversation.store.project
    sid = conversation.store.id
    conversation.store.close()
    restored = open_conversation(project)
    assert restored.store.id == sid
    assert "执行环境已切换" in restored.notice


def test_language_server_timeout_blocks_later_calls(tmp_path, monkeypatch):
    backend = NativeBackend.__new__(NativeBackend)
    backend.workspace = tmp_path
    backend.read_paths = ()
    backend.healthy = True
    payload = json.dumps({"success": False, "data": {}, "error_code": "LSP_TIMEOUT",
                          "error": "Language server timed out"})
    monkeypatch.setattr(backend, "_run", lambda **kw: ProcessResult(
        0, payload, "", False, None, 1, False, False,
    ))
    result = backend.execute(tmp_path, "get_symbols", {"path": "example.py"})
    assert result.error_code == "LSP_TIMEOUT"
    assert not backend.healthy
    assert backend.execute(tmp_path, "write_file", {}).error_code == "NATIVE_UNHEALTHY"


REAL_NATIVE = pytest.mark.skipif(
    sys.platform != "darwin" or os.getenv("RUN_SANDBOX_NATIVE_TESTS") != "1",
    reason="Requires macOS Seatbelt outside an enclosing sandbox; set RUN_SANDBOX_NATIVE_TESTS=1",
)


@pytest.fixture
def bare_backend(tmp_path):
    backend = NativeBackend.__new__(NativeBackend)
    backend.workspace = tmp_path
    backend.read_paths = ()
    backend.python = Path(sys.executable)
    backend.healthy = True
    backend.last_cleanup = {"cleanup_status": "unknown"}
    return backend


def test_command_validation_and_output_bypass_worker_protocol(bare_backend, monkeypatch):
    backend = bare_backend
    calls = []

    def run(command=None, **kwargs):
        calls.append((command, kwargs))
        return ProcessResult(None, "partial", "warning", True, None, 1000, False, False,
                             status="timed_out", cleanup_status="confirmed", output_complete=False)

    monkeypatch.setattr(backend, "_run", run)
    invalid = backend.execute(backend.workspace, "run_command", {"command": ["x"], "cwd": "../"})
    assert invalid.error_code == "PATH_OUTSIDE_WORKSPACE" and calls == []
    result = backend.execute(backend.workspace, "run_python", {"code": "print('x')", "timeout_seconds": 1})
    command, kwargs = calls[0]
    assert command == [str(backend.python), "-u", "-c", "print('x')"]
    assert "request" not in kwargs
    assert kwargs["timeout"] == 1 and kwargs["cwd"] == backend.workspace
    assert result.data["stdout"] == "partial" and result.data["stderr"] == "warning"
    assert result.data["execution_allowed"] and backend.healthy


def test_degraded_backend_keeps_sandboxed_reads_and_passive_environment(bare_backend, monkeypatch):
    backend = bare_backend
    backend.healthy = False
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return ProcessResult(0, json.dumps({"success": True, "data": {"files": []}}),
                             "", False, None, 1, False, False, cleanup_status="confirmed")

    monkeypatch.setattr(backend, "_run", run)
    assert backend.execute(backend.workspace, "read_file", {"reads": [{"path": "a.py"}]}).success
    assert calls[0]["request"]["name"] == "read_file"
    assert not backend.healthy  # A successful read does not clear quarantine.
    assert backend.execute(backend.workspace, "run_command", {"command": ["echo"]}).error_code == "NATIVE_UNHEALTHY"
    monkeypatch.setattr("tools.execute.ProcessRunner.run", lambda *a, **kw: pytest.fail("Active environment probe"))
    result = backend.execute(backend.workspace, "get_execution_environment", {})
    assert result.success
    assert result.data["executor_healthy"] is False
    assert result.data["execution"]["mode"] == "native"
    assert result.data["execution"]["command_execution_allowed"] is False
    assert len(calls) == 1


def test_worker_failure_retains_bounded_output(bare_backend, monkeypatch):
    backend = bare_backend
    monkeypatch.setattr(backend, "_run", lambda **kw: ProcessResult(
        None, "first" + "x" * 50000 + "last", "error", True, None, 130000,
        False, False, status="timed_out", cleanup_status="confirmed", output_complete=False,
    ))
    result = backend.execute(backend.workspace, "list_files", {})
    assert result.error_code == "NATIVE_EXECUTION_FAILED"
    assert result.data["stdout"].startswith("first") and result.data["stdout"].endswith("last")
    assert result.data["stdout_truncated"] and len(result.data["stdout"]) < 33000
    assert result.data["stderr"] == "error" and not result.data["output_complete"]


def test_outer_supervisor_confirmation_recovers_inner_timeout(bare_backend, monkeypatch):
    payload = json.dumps({"success": False, "data": {"cleanup_error": "inner permission denied"},
                          "error_code": "LSP_TIMEOUT", "error": "timeout"})
    monkeypatch.setattr(bare_backend, "_run", lambda **kw: ProcessResult(
        0, payload, "", False, None, 1, False, False, cleanup_status="confirmed",
    ))
    result = bare_backend.execute(bare_backend.workspace, "get_symbols", {"path": "a.py"})
    assert result.error_code == "LSP_TIMEOUT"
    assert result.data["inner_cleanup_error"] == "inner permission denied"
    assert result.data["cleanup_error"] is None and bare_backend.healthy


@pytest.fixture
def native_project(tmp_path):
    workspace = tmp_path / 'project "quoted" (中文)'
    workspace.mkdir()
    backend = NativeBackend(workspace)
    try:
        yield workspace, backend
    finally:
        backend.close()


@REAL_NATIVE
def test_native_edits_original_runs_tools_and_reports_actual_limits(native_project):
    root, backend = native_project
    tools = {tool.definition.name: tool for tool in backend.tools()}
    assert {"run_command", "run_python", "get_symbols"} <= tools.keys()
    assert tools["write_file"].execute({"path": "test.txt", "content": "original"}).success
    assert (root / "test.txt").read_text() == "original"
    result = tools["run_python"].execute({"code": "from pathlib import Path; Path('test.txt').write_text('edited')"})
    assert result.success and result.data["exit_code"] == 0, result
    assert (root / "test.txt").read_text() == "edited"
    result = tools["run_command"].execute({"command": ["/bin/echo", "ok"]})
    assert result.data["stdout"].strip() == "ok", result
    report = tools["get_execution_environment"].execute({})
    assert report.success, report
    assert report.data["execution"]["mode"] == "native"
    assert report.data["execution"]["writeback_mode"] == "direct"
    assert report.data["execution"]["resources"]["memory_limit"] is None
    assert report.data["runtimes"]["python"]["status"] == "available"


@REAL_NATIVE
def test_scripts_cannot_escape_or_read_credentials(native_project, tmp_path, monkeypatch):
    root, backend = native_project
    outside = tmp_path / "outside.txt"
    outside.write_text("not accessible")
    (root / "alias").symlink_to(outside)
    (root / ".ENV").write_text("secret")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    code = f'''
import errno, os, pathlib, socket, subprocess, sys
def denied(action):
    try:
        action()
    except OSError as e:
        assert e.errno in (errno.EPERM, errno.EACCES), repr(e)
    else:
        raise AssertionError('operation escaped')
outside = pathlib.Path({str(outside)!r})
for path in (outside, pathlib.Path('alias'), pathlib.Path('.ENV'), pathlib.Path('.git/config')):
    denied(path.read_text)
    denied(lambda: path.write_text('changed'))
denied(lambda: pathlib.Path({str(backend.runtime / 'tools/factory.py')!r}).write_text('changed'))
denied(lambda: socket.socket().connect(('127.0.0.1', 9)))
denied(lambda: pathlib.Path('new.key').write_text('secret'))
assert 'OPENAI_API_KEY' not in os.environ
child = subprocess.run([sys.executable, '-I', '-c', 'open(' + repr(str(outside)) + ').read()'], capture_output=True)
assert child.returncode != 0
'''
    result = backend.execute(root, "run_python", {"code": code})
    assert result.success and result.data["exit_code"] == 0, result
    assert outside.read_text() == "not accessible"
    assert (root / ".ENV").read_text() == "secret"


@REAL_NATIVE
def test_git_read_tools_work_but_scripts_cannot_modify_metadata(native_project):
    root, backend = native_project
    subprocess.run(["/usr/bin/git", "init", "-q", str(root)], check=True)
    (root / "new.txt").write_text("new")
    result = backend.execute(root, "git_status", {})
    assert result.success, result
    result = backend.execute(root, "git_diff", {})
    assert result.success, result


@REAL_NATIVE
def test_hardlink_and_failed_cleanup_stop_execution(native_project, tmp_path, monkeypatch):
    root, backend = native_project
    outside = tmp_path / "outside"
    outside.write_text("original")
    os.link(outside, root / "hardlink")
    with pytest.raises(ValueError, match="硬链接"):
        backend.execute(root, "run_command", {"command": ["/bin/true"]})
    (root / "hardlink").unlink()
    with monkeypatch.context() as scoped:
        scoped.setattr("sandbox.native.ProcessRunner.run", lambda *a, **kw: ProcessResult(
            0, "partial output", "", False, "cleanup failed", 1, False, False,
            cleanup_status="unknown", output_complete=False,
        ))
        result = backend.execute(root, "run_command", {"command": ["/bin/true"]})
        assert not result.success and result.data["stdout"] == "partial output"
    assert not backend.healthy
    assert backend.execute(root, "write_file", {}).error_code == "NATIVE_UNHEALTHY"
    (root / "readable.txt").write_text("still readable")
    result = backend.execute(root, "read_file", {"reads": [{"path": "readable.txt"}]})
    assert result.success, result
    assert not backend.healthy


@REAL_NATIVE
def test_custom_protected_paths_and_runtime_ancestors_cannot_be_renamed(native_project):
    root, backend = native_project
    private = root / "parent" / "custom-secret"
    private.parent.mkdir()
    private.write_text("secret")
    runtime = root / "toolchain" / "lib"
    runtime.mkdir(parents=True)
    (runtime / "module.py").write_text("trusted")
    backend.protected_paths = (*backend.protected_paths, private)
    backend.read_paths = (*backend.read_paths, runtime)
    code = '''
import errno
from pathlib import Path
for path in ('parent', 'toolchain'):
    try:
        Path(path).rename(path + '-moved')
    except OSError as e:
        assert e.errno in (errno.EPERM, errno.EACCES), repr(e)
    else:
        raise AssertionError('protected ancestor renamed')
'''
    result = backend.execute(root, "run_python", {"code": code})
    assert result.success and result.data["exit_code"] == 0, result


@REAL_NATIVE
def test_timeout_keeps_output_and_allows_later_calls(native_project):
    root, backend = native_project
    result = backend.execute(root, "run_python", {
        "code": "import time,sys; print('started', flush=True); print('progress', file=sys.stderr, flush=True); time.sleep(10)", "timeout_seconds": 1,
    })
    assert result.data["timed_out"], result
    assert result.data["stdout"] == "started\n"
    assert result.data["stderr"] == "progress\n"
    assert result.data["cleanup_status"] == "confirmed"
    assert result.data["output_complete"] is False
    assert backend.healthy
    assert backend.execute(root, "run_command", {"command": ["/bin/echo", "after timeout"]}).data["stdout"] == "after timeout\n"


@REAL_NATIVE
def test_native_timeout_cleans_child_that_changes_session(native_project):
    from tools.process_supervisor import ProcessTable

    root, backend = native_project
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(10)"
    code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-u','-c',{child!r}], start_new_session=True); "
        "print(p.pid, flush=True); time.sleep(10)"
    )
    result = backend.execute(root, "run_python", {"code": code, "timeout_seconds": 1})
    assert result.data["timed_out"] and "ready" in result.data["stdout"]
    assert result.data["cleanup_status"] == "confirmed", result
    pid = int(result.data["stdout"].splitlines()[0])
    info = ProcessTable().read(pid)
    assert info is None or info.zombie
    assert backend.healthy


@REAL_NATIVE
def test_native_cli_records_mode_and_closes_backend(tmp_path, monkeypatch):
    from agent.session import SessionStore
    from cli import main as cli
    from llm import LLMClient

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("AGENT_SANDBOX_WRITEBACK", "on-success")
    monkeypatch.setattr(sys, "argv", ["repo-agent", "--root", str(tmp_path),
                                      "--model", "m"])
    monkeypatch.setattr(LLMClient, "get_context_limit", lambda *a, **kw: None)
    backends = []

    def interactive(runtime, **kwargs):
        conversation = kwargs["conversation"]
        assert conversation.mode == "native" and kwargs.get("sandbox") is None
        backend = conversation.execution_backend
        backends.append(backend)
        result = backend.execute(tmp_path, "run_python", {"code": "print('native cli')"})
        assert result.success and result.data["exit_code"] == 0, result

    monkeypatch.setattr(cli, "run_interactive", interactive)
    cli.main()
    assert len(backends) == 1 and not backends[0].directory.exists()
    store = SessionStore(tmp_path).open()
    try:
        assert store.data["mode"] == "native" and store.data["sandbox"] is None
    finally:
        store.close()
