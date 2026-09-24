"""Linux namespace policy and opt-in real kernel enforcement tests."""

import errno
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.linux_native import LinuxNativeBackend
from sandbox.native import NativeBackend


def test_factory_selects_linux_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("sandbox.native.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(LinuxNativeBackend, "__init__", lambda self, root: None)
    assert isinstance(NativeBackend(tmp_path), LinuxNativeBackend)


def test_missing_bubblewrap_never_falls_back(monkeypatch):
    monkeypatch.setattr("sandbox.linux_native.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr("sandbox.linux_native.shutil.which", lambda *a, **kw: None)
    with pytest.raises(ValueError, match="缺少 bubblewrap"):
        LinuxNativeBackend("/not-used")


@pytest.fixture
def linux_policy(tmp_path):
    backend = object.__new__(LinuxNativeBackend)
    backend.workspace = tmp_path / "project"
    backend.workspace.mkdir()
    backend.runtime = tmp_path / "runtime"
    backend.runtime.mkdir()
    backend.python = Path("/usr/bin/python3")
    backend.read_paths = (backend.runtime,)
    backend.protected_paths = ()
    backend.executable = Path("/usr/bin/bwrap")
    return backend


def test_policy_masks_secrets_and_readonly_git(linux_policy, tmp_path):
    backend = linux_policy
    (backend.workspace / ".ENV").write_text("private")
    git = backend.workspace / ".git"
    git.mkdir()
    (git / "config").write_text("metadata")
    (git / "logs").mkdir()
    masks, git_paths = backend._mount_policy(backend.read_paths, git_read=False)
    assert set(masks) == {backend.workspace / ".ENV", git}
    assert git_paths == []
    masks, git_paths = backend._mount_policy(backend.read_paths, git_read=True)
    assert set(masks) == {backend.workspace / ".ENV", git / "logs"}
    assert git_paths == [git]
    control = tmp_path / "call"
    control.mkdir()
    scratch = control / "scratch"
    scratch.mkdir()
    argv = backend._sandbox_command(["echo", "ok"], control, scratch, backend.read_paths)
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--disable-userns",
                 "--assert-userns-disabled", "--die-with-parent", "--new-session"):
        assert flag in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert str(backend.runtime / "sandbox/linux_exec.py") in argv
    assert "--bind" in argv and "--ro-bind" in argv
    assert (control / "hidden-file").stat().st_mode & 0o777 == 0
    assert (control / "hidden-dir").stat().st_mode & 0o777 == 0
    (control / "hidden-dir").chmod(0o700)  # Let non-root pytest remove its fixture.


def test_masks_rescan_and_reject_protected_symlinks(linux_policy):
    root = linux_policy.workspace
    assert linux_policy._mount_policy((), git_read=False) == ([], [])
    (root / "new.key").write_text("new")
    assert linux_policy._mount_policy((), git_read=False)[0] == [root / "new.key"]
    (root / ".env").symlink_to("new.key")
    with pytest.raises(ValueError, match="符号链接"):
        linux_policy._mount_policy((), git_read=False)


def test_configured_paths_and_toolchain_aliases_are_hidden(linux_policy):
    backend = linux_policy
    private = backend.workspace / "custom-private"
    private.mkdir()
    backend.protected_paths = (private,)
    certdir = backend.runtime / "certs"
    certdir.mkdir()
    (certdir / "cert.pem").symlink_to("/elsewhere")
    masks, _ = backend._mount_policy(backend.read_paths, git_read=False)
    assert set(masks) == {private, certdir}


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("unreadable_root", [False, True])
def test_unreadable_readonly_tree_is_masked(linux_policy, tmp_path, monkeypatch,
                                          error_number, unreadable_root):
    system = tmp_path / "system"
    restricted = system if unreadable_root else system / "modules/kernel/lost+found"
    restricted.mkdir(parents=True)
    (restricted / ".env").write_text("must never be exposed")
    original = os.scandir

    def scandir(path):
        if Path(path) == restricted:
            raise PermissionError(error_number, "Permission denied", str(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", scandir)
        masks, _ = linux_policy._mount_policy((system,), git_read=False)
        assert masks == [restricted]
        control = tmp_path / "call"
        control.mkdir()
        scratch = control / "scratch"
        scratch.mkdir()
        argv = linux_policy._sandbox_command(["true"], control, scratch, (system,))
        assert any(argv[i:i + 3] == ["--ro-bind", str(control / "hidden-dir"), str(restricted)]
                   for i in range(len(argv)))
        (control / "hidden-dir").chmod(0o700)


@pytest.mark.parametrize("readonly", [False, True])
def test_unreadable_workspace_still_fails_closed(linux_policy, monkeypatch, readonly):
    restricted = linux_policy.workspace / "private"
    restricted.mkdir()
    original = os.scandir

    def scandir(path):
        if Path(path) == restricted:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", scandir)
        with pytest.raises(PermissionError):
            linux_policy._mount_policy((restricted,) if readonly else (), git_read=False)
        with pytest.raises(PermissionError):
            linux_policy._check_workspace()


def test_unreadable_workspace_alias_is_not_treated_as_system_tree(linux_policy, tmp_path, monkeypatch):
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(linux_policy.workspace, target_is_directory=True)
    original = os.scandir

    def scandir(path):
        if Path(path) == alias:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", scandir)
        with pytest.raises(PermissionError):
            linux_policy._mount_policy((alias,), git_read=False)


@pytest.mark.parametrize("error_number", [errno.EIO, errno.ENOENT])
def test_system_scan_other_errors_are_not_ignored(linux_policy, monkeypatch, error_number):
    original = os.scandir

    def scandir(path):
        if Path(path) == linux_policy.runtime:
            raise OSError(error_number, "scan failed", str(path))
        return original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "scandir", scandir)
        with pytest.raises(OSError) as caught:
            linux_policy._mount_policy(linux_policy.read_paths, git_read=False)
        assert caught.value.errno == error_number


def test_linux_dependency_checks_and_manual_toolchain_hint(monkeypatch):
    from cli import dependencies

    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(dependencies.shutil, "which", lambda *a, **kw: None)
    assert dependencies.native_preflight()[0][0] == "ERROR"
    monkeypatch.setattr(dependencies.shutil, "which", lambda *a, **kw: "/usr/bin/bwrap")
    monkeypatch.setattr("ctypes.CDLL", lambda *a, **kw: object())
    assert dependencies.native_preflight()[0][0] == "OK"
    monkeypatch.setattr(dependencies, "toolchain_report", lambda *a: ([], ["python"]))
    with pytest.raises(ValueError, match="发行版包管理器"):
        dependencies.prepare_toolchains("/usr/bin/python3", "go")


REAL_LINUX = pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("RUN_SANDBOX_LINUX_TESTS") != "1",
    reason="Requires Linux bubblewrap/libseccomp and usable user namespaces",
)


@pytest.fixture
def linux_project(tmp_path):
    root = tmp_path / 'project "quoted" (中文)'
    root.mkdir()
    backend = NativeBackend(root)
    try:
        yield root, backend
    finally:
        backend.close()


@REAL_LINUX
@pytest.mark.parametrize("mode", [0o000, 0o111])
def test_real_unreadable_system_directory_is_hidden_on_all_mount_paths(tmp_path, monkeypatch, mode):
    if os.geteuid() == 0:
        pytest.skip("Permission regression must run as an ordinary user")
    root = tmp_path / "project"
    root.mkdir()
    system = tmp_path / "usr"
    lib = system / "lib"
    restricted = lib / "modules/6.6.87.2-microsoft-standard-WSL2/lost+found"
    restricted.mkdir(parents=True)
    (restricted / ".env").write_text("secret")
    (lib / "ordinary.txt").write_text("readable")
    alias = tmp_path / "lib"
    alias.symlink_to(lib, target_is_directory=True)
    original = LinuxNativeBackend._read_paths
    monkeypatch.setattr(LinuxNativeBackend, "_read_paths", lambda self: (*original(self), system, alias))
    backend = None
    restricted.chmod(mode)
    try:
        if mode == 0o111:
            assert (restricted / ".env").read_text() == "secret"  # Can't list, but CAN read known files.
        backend = NativeBackend(root, profile="standard")  # Startup itself must now succeed.
        paths = [restricted, alias / restricted.relative_to(lib)]
        code = f'''
import pathlib
assert pathlib.Path({str(lib / 'ordinary.txt')!r}).read_text() == 'readable'
for directory in {list(map(str, paths))!r}:
    path = pathlib.Path(directory)
    for operation in (lambda: list(path.iterdir()), lambda: (path / '.env').read_text(),
                      lambda: path.chmod(0o755), lambda: (path / 'new.txt').write_text('no')):
        try: operation()
        except OSError: pass
        else: raise AssertionError('unscanned directory accessible: ' + directory)
print('masked')
'''
        outcome = backend.execute(root, "run_python", {"code": code})
        assert outcome.success and outcome.data["exit_code"] == 0, outcome
        assert outcome.data["stdout"] == "masked\n"
    finally:
        restricted.chmod(0o700)
        if backend is not None:
            backend.close()
    assert (restricted / ".env").read_text() == "secret"


@REAL_LINUX
def test_real_linux_tools_and_environment(linux_project):
    root, backend = linux_project
    assert isinstance(backend, LinuxNativeBackend)
    assert backend.execute(root, "write_file", {"path": "a.py", "content": "x = 1\n"}).success
    assert (root / "a.py").read_text() == "x = 1\n"
    result = backend.execute(root, "run_python", {"code": "from pathlib import Path; Path('a.py').write_text('x = 2\\n')"})
    assert result.success and result.data["exit_code"] == 0, result
    assert (root / "a.py").read_text() == "x = 2\n"
    report = backend.execute(root, "get_execution_environment", {})
    assert report.success, report
    assert report.data["execution"]["platform"] == "linux"
    assert report.data["execution"]["isolation"] == "bubblewrap+seccomp"
    assert report.data["execution"]["network"] == "disabled"
    assert report.data["execution"]["resources"]["memory_limit"] is None
    if importlib.util.find_spec("pylsp"):
        result = backend.execute(root, "get_symbols", {"path": "a.py"})
        assert result.success, result


@REAL_LINUX
def test_real_linux_files_network_processes_and_inheritance(linux_project, tmp_path, monkeypatch):
    root, backend = linux_project
    outside = tmp_path / "outside"
    outside.write_text("host secret")
    (root / ".ENV").write_text("project secret")
    (root / "alias").symlink_to(outside)
    (root / "secret-alias").symlink_to(root / ".ENV")
    (root / "ordinary").write_text("public")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("metadata")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-inherit")
    code = f'''
import errno, os, pathlib, socket, subprocess, sys
def denied(action):
    try: action()
    except OSError as e:
        assert e.errno in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS), repr(e)
    else: raise AssertionError('operation allowed')
for path in ({str(outside)!r}, 'alias', 'secret-alias', '.ENV', '.git/config'):
    p = pathlib.Path(path)
    denied(p.read_text)
    denied(lambda: p.write_text('changed'))
denied(lambda: pathlib.Path({str(backend.runtime / 'tools/factory.py')!r}).write_text('changed'))
denied(lambda: pathlib.Path('/usr/bin/native-test').write_text('changed'))
denied(lambda: os.link('ordinary', 'hardlink'))
denied(lambda: socket.socket())
denied(lambda: socket.socket(socket.AF_UNIX))
left, right = socket.socketpair()
left.send(b'private'); assert right.recv(7) == b'private'
left.close(); right.close()
assert 'OPENAI_API_KEY' not in os.environ
child = subprocess.run([sys.executable, '-I', '-c', 'import socket; socket.socket()'], capture_output=True)
assert child.returncode != 0
assert 'NoNewPrivs:\\t1' in pathlib.Path('/proc/self/status').read_text()
'''
    result = backend.execute(root, "run_python", {"code": code})
    assert result.success and result.data["exit_code"] == 0, result
    assert outside.read_text() == "host secret"
    assert (root / ".ENV").read_text() == "project secret"


@REAL_LINUX
def test_real_linux_node_child_process_and_typescript(linux_project):
    root, backend = linux_project
    if not shutil.which("typescript-language-server", path=backend._environment(root)["PATH"]):
        pytest.skip("TypeScript language server is not installed")
    (root / "example.ts").write_text("export function example(): number { return 1; }\n")
    result = backend.execute(root, "get_symbols", {"path": "example.ts"})
    assert result.success, result
    assert any(symbol["name"] == "example" for symbol in result.data["symbols"])


@REAL_LINUX
def test_real_linux_git_readonly_and_new_secret_boundary(linux_project):
    root, backend = linux_project
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "a.txt").write_text("new")
    for name in ("git_status", "git_diff"):
        result = backend.execute(root, name, {})
        assert result.success, result
    # Kernel mount policy is a snapshot; application file tools still reject names.
    assert not backend.execute(root, "write_file", {"path": "new.key", "content": "x"}).success
    result = backend.execute(root, "run_python", {"code": "from pathlib import Path; Path('new.key').write_text('x')"})
    assert result.data["exit_code"] == 0, result
    result = backend.execute(root, "run_python", {"code": "open('new.key').read()"})
    assert result.data["exit_code"] != 0, result


@REAL_LINUX
def test_real_linux_timeout_cleans_detached_children(linux_project):
    root, backend = linux_project
    child = """import signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('child ready', flush=True)
while True:
    Path('heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(0.02)
"""
    code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child!r}], start_new_session=True); print('started',flush=True); time.sleep(30)"
    result = backend.execute(root, "run_python", {"code": code, "timeout_seconds": 2})
    assert result.data["timed_out"] and "started" in result.data["stdout"], result
    assert result.data["cleanup_status"] == "confirmed", result
    assert "child ready" in result.data["stdout"]
    assert result.data["output_complete"] is False
    heartbeat = (root / "heartbeat").read_bytes()
    time.sleep(0.15)
    assert (root / "heartbeat").read_bytes() == heartbeat
    assert backend.healthy
    assert backend.execute(root, "run_command", {"command": ["/bin/echo", "after"]}).data["stdout"] == "after\n"


@REAL_LINUX
def test_real_linux_normal_exit_removes_fast_daemon(linux_project):
    root, backend = linux_project
    child = """import os,signal,time
from pathlib import Path
os.setsid()
if os.fork():
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    Path('daemon-heartbeat').write_text(str(time.monotonic_ns()))
    time.sleep(0.02)
"""
    code = f"""import subprocess,sys,time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-c', {child!r}],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
p.wait()
deadline = time.monotonic() + 5
while not Path('daemon-heartbeat').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('child did not start')
    time.sleep(0.01)
print('parent done')
"""
    result = backend.execute(root, "run_python", {"code": code})
    assert result.data["exit_code"] == 0 and not result.data["timed_out"], result
    assert result.data["cleanup_status"] == "confirmed"
    heartbeat = (root / "daemon-heartbeat").read_bytes()
    time.sleep(0.15)
    assert (root / "daemon-heartbeat").read_bytes() == heartbeat
    assert backend.healthy


@REAL_LINUX
def test_real_linux_continuous_output_has_hard_deadline_and_keeps_both_streams(linux_project):
    root, backend = linux_project
    code = """import sys,time
print('stdout begin', flush=True)
print('stderr begin', file=sys.stderr, flush=True)
while True:
    print('x' * 8192, flush=True)
    print('y' * 8192, file=sys.stderr, flush=True)
    time.sleep(0.01)
"""
    result = backend.execute(root, "run_python", {"code": code, "timeout_seconds": 1})
    assert result.data["status"] == "timed_out", result
    assert result.data["duration_ms"] < 4500
    assert result.data["cleanup_status"] == "confirmed"
    for stream in ("stdout", "stderr"):
        assert result.data[stream].startswith(stream + " begin")
        assert result.data[stream + "_truncated"]
        assert len(result.data[stream]) < 33000
    assert not result.data["output_complete"] and backend.healthy


@REAL_LINUX
def test_real_linux_cancellation_preserves_backend_health(linux_project, monkeypatch):
    from tools._internal.process_runner import ProcessRunner

    root, backend = linux_project
    collect = ProcessRunner._collect_output

    def cancel(process, streams, deadline, supervisor=None):
        collect(process, streams, min(deadline, time.monotonic() + 0.2), supervisor)
        raise KeyboardInterrupt

    with monkeypatch.context() as scoped:
        scoped.setattr(ProcessRunner, "_collect_output", staticmethod(cancel))
        with pytest.raises(KeyboardInterrupt):
            backend.execute(root, "run_python", {"code": "import time; time.sleep(30)"})
    assert backend.healthy
    assert backend.execute(root, "run_python", {"code": "print('recovered')"}).data["stdout"] == "recovered\n"


@REAL_LINUX
def test_real_linux_non_utf8_process_name_does_not_break_cleanup(linux_project):
    root, backend = linux_project
    code = """import ctypes,time
libc = ctypes.CDLL(None, use_errno=True)
assert libc.prctl(15, ctypes.c_char_p(b'worker)\\xff'), 0, 0, 0) == 0
print('renamed', flush=True)
time.sleep(0.2)
"""
    result = backend.execute(root, "run_python", {"code": code})
    assert result.data["exit_code"] == 0 and result.data["stdout"] == "renamed\n", result
    assert result.data["cleanup_status"] == "confirmed" and backend.healthy


@REAL_LINUX
def test_real_linux_rejects_preexisting_hardlinks(linux_project, tmp_path):
    root, backend = linux_project
    outside = tmp_path / "linked"
    outside.write_text("secret")
    os.link(outside, root / "link")
    with pytest.raises(ValueError, match="硬链接"):
        backend.execute(root, "run_command", {"command": ["/bin/true"]})


@REAL_LINUX
def test_real_linux_protected_ancestors_cannot_be_moved(linux_project):
    root, backend = linux_project
    private = root / "parent" / "private-data"
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
for name in ('parent', 'toolchain'):
    try: Path(name).rename(name + '-moved')
    except OSError as e:
        assert e.errno in (errno.EBUSY, errno.EPERM, errno.EACCES, errno.EROFS), repr(e)
    else: raise AssertionError('protected ancestor moved')
'''
    result = backend.execute(root, "run_python", {"code": code})
    assert result.data["exit_code"] == 0, result


@REAL_LINUX
def test_real_linux_default_cli_and_session(tmp_path, monkeypatch):
    from agent.session import SessionStore
    from cli import main as cli
    from llm import LLMClient

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(sys, "argv", ["repo-agent", "--root", str(tmp_path), "--model", "m"])
    monkeypatch.setattr(LLMClient, "get_context_limit", lambda *a, **kw: None)
    backends = []

    def interactive(runtime, **kwargs):
        backend = kwargs["conversation"].execution_backend
        assert isinstance(backend, LinuxNativeBackend)
        backends.append(backend)
        result = backend.execute(tmp_path, "run_command", {"command": ["/bin/echo", "linux cli"]})
        assert result.success and result.data["stdout"] == "linux cli\n", result

    monkeypatch.setattr(cli, "run_interactive", interactive)
    cli.main()
    assert len(backends) == 1 and not backends[0].directory.exists()
    store = SessionStore(tmp_path).open()
    try:
        assert store.data["mode"] == "native" and store.data["sandbox"] is None
    finally:
        store.close()
