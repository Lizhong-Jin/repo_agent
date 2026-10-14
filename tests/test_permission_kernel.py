"""Opt-in real OS enforcement; mocked contracts do not establish kernel isolation."""

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from sandbox.native import create_native_backend
from tools.permissions import PermissionManager

FLAG = {
    "darwin": "RUN_SANDBOX_NATIVE_TESTS",
    "linux": "RUN_SANDBOX_LINUX_TESTS",
    "win32": "RUN_WINDOWS_ISOLATION_TESTS",
}.get(sys.platform)
pytestmark = pytest.mark.skipif(
    FLAG is None or os.getenv(FLAG) != "1",
    reason="Requires explicit real native OS isolation opt-in",
)


@pytest.fixture
def native_inputs(tmp_path, monkeypatch):
    workspace, external = tmp_path / "project", tmp_path / "external 输入 空格"
    workspace.mkdir()
    external.mkdir()
    (external / "sample.txt").write_text("authorized-input")
    (external / ".env").write_text("never-visible")
    (external / "run.py").write_text("print('external-script-ran')\n")
    monkeypatch.setattr("sandbox.windows_native.app_directory", lambda _: tmp_path / "state")
    backend = create_native_backend(workspace, profile="standard")
    manager = PermissionManager(workspace, backend="native")
    manager.bind_backend(backend)
    identity = manager.grant("external_read", str(external))
    try:
        yield backend, manager, external, identity
    finally:
        backend.close()
        if sys.platform == "win32":
            from host_support.windows_recovery import recover_profiles

            # Only fixture-owned retained records; never discard unrelated calls.
            for record in backend.windows_state_root.glob("*.json"):
                result = recover_profiles(
                    backend.windows_state_root, backend.windows_api, discard=record.stem
                )
                assert not result["failed"], result


def test_native_external_read_execute_write_denial_and_revoke(native_inputs):
    backend, manager, external, identity = native_inputs
    code = f"""
import json, os
from pathlib import Path
p = Path(json.loads(os.environ['REPO_AGENT_READ_ROOTS'])[{identity!r}])
assert (p / 'sample.txt').read_text() == 'authorized-input'
assert not (p / '.env').exists()
for target in (p / 'sample.txt', p / 'new.txt', Path({str(external / "sample.txt")!r})):
    try:
        target.write_text('not-allowed')
    except OSError:
        pass
    else:
        raise AssertionError('write escaped: ' + str(target))
Path('result.txt').write_text((p / 'sample.txt').read_text())
print(str(p), flush=True)
"""
    result = backend.execute(backend.workspace, "run_python", {"code": code})
    assert result.success and result.data["exit_code"] == 0, result
    assert (backend.workspace / "result.txt").read_text() == "authorized-input"
    assert not Path(result.data["stdout"].strip()).exists()
    assert (external / "sample.txt").read_text() == "authorized-input"
    script = backend.execute(
        backend.workspace, "run_command", {"command": ["python", str(external / "run.py")]}
    )
    assert script.success and "external-script-ran" in script.data["stdout"], script
    manager.revoke(identity)
    denied = backend.execute(
        backend.workspace, "run_command", {"command": ["python", str(external / "run.py")]}
    )
    assert not denied.success or denied.data["exit_code"] != 0, denied
    assert backend.healthy


def test_native_input_copy_removed_after_timeout(native_inputs):
    backend, _, _, _ = native_inputs
    result = backend.execute(
        backend.workspace,
        "run_python",
        {
            "code": "import os,json,time; print(next(iter(json.loads("
            "os.environ['REPO_AGENT_READ_ROOTS']).values())),flush=True); time.sleep(30)",
            "timeout_seconds": 3,
        },
    )
    assert result.data["timed_out"], result
    copied = result.data["stdout"].strip()
    assert copied and not Path(copied).exists(), result
    assert backend.healthy


def test_native_running_cancellation_removes_inputs_after_process_cleanup(
    native_inputs, monkeypatch
):
    import sandbox.permission_inputs as inputs

    backend, _, _, _ = native_inputs
    context = CancellationContext()
    staged = []
    markers = []
    began = threading.Event()
    finished = threading.Event()
    original = inputs.stage_permission_inputs

    def capture(*args, **kwargs):
        paths, mapping = original(*args, **kwargs)
        staged.extend(Path(p) for p in paths.values())
        workspace = (
            Path(next(iter(paths.values()))).parent.parent / "workspace"
            if sys.platform == "win32"
            else backend.workspace
        )
        markers.append(workspace / "started.txt")
        return paths, mapping

    def cancel_when_running():
        deadline = time.monotonic() + 30
        while not finished.wait(0.02):
            if markers and markers[0].exists():
                began.set()
                context.cancel()
                return
            if time.monotonic() >= deadline:
                context.cancel()
                return

    monkeypatch.setattr(inputs, "stage_permission_inputs", capture)
    if sys.platform != "win32":
        monkeypatch.setattr("sandbox.posix_execution.stage_permission_inputs", capture)
    watcher = threading.Thread(target=cancel_when_running)
    watcher.start()
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled):
            backend.execute(
                backend.workspace,
                "run_python",
                {
                    "code": "from pathlib import Path; import time; "
                    "Path('started.txt').write_text('running'); time.sleep(60)",
                    "timeout_seconds": 30,
                },
            )
    finally:
        finished.set()
        watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert began.is_set(), "Cancellation must occur after the child actually started"
    assert staged and all(not path.exists() for path in staged)
    assert backend.healthy


def test_native_input_copy_removed_after_cancellation(native_inputs, monkeypatch):
    import sandbox.permission_inputs as inputs

    backend, _, _, _ = native_inputs
    context = CancellationContext()
    staged = []
    original = inputs.stage_permission_inputs

    def capture(*args, **kwargs):
        paths, mapping = original(*args, **kwargs)
        staged.extend(Path(p) for p in paths.values())
        # Cancellation after staging exercises release of acquired input resources.
        context.cancel()
        return paths, mapping

    monkeypatch.setattr(inputs, "stage_permission_inputs", capture)
    if sys.platform != "win32":
        monkeypatch.setattr("sandbox.posix_execution.stage_permission_inputs", capture)
    with cancellation_scope(context), pytest.raises(RunCancelled):
        backend.execute(backend.workspace, "run_python", {"code": "import time; time.sleep(30)"})
    assert staged and all(not path.exists() for path in staged)
    assert backend.healthy
