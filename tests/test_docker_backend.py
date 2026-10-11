"""Docker diagnostics and Windows-compatible mount arguments without a daemon."""

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support.processes import ProcessResult
from sandbox.docker import DockerBackend
from sandbox.policy import SandboxPolicy


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr("sandbox.docker.shutil.which", lambda _: "docker")
    monkeypatch.setattr(
        "sandbox.docker.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="sha256:test", stderr=b""),
    )
    return DockerBackend(SandboxPolicy())


def result(**values):
    fields = dict(
        exit_code=0,
        stdout='{"success": true}',
        stderr="",
        timed_out=False,
        cleanup_error=None,
        duration_ms=1,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    return ProcessResult(**(fields | values))


@pytest.mark.parametrize("path", ["中文 workspace", "comma,dir", 'quote"dir'])
def test_mounts_preserve_paths_as_csv_fields(backend, tmp_path, monkeypatch, path):
    workspace = tmp_path / path

    def run(command, **kwargs):
        mounts = [command[i + 1] for i, item in enumerate(command) if item == "--mount"]
        assert next(csv.reader([mounts[0]])) == [
            "type=bind",
            f"src={workspace}",
            "dst=/workspace",
        ]
        request_mount = next(csv.reader([mounts[1]]))
        assert request_mount[-2:] == ["dst=/request.json", "readonly"]
        request = json.loads(Path(request_mount[1].removeprefix("src=")).read_text())
        assert request["name"] == "git_status"
        assert command[-1] == "sha256:test"
        return result()

    monkeypatch.setattr(backend.runner, "run", run)
    assert backend.execute(workspace, "git_status", {}).success


@pytest.mark.parametrize(
    "failure",
    [
        {"exit_code": 125, "stderr": "invalid mount config"},
        {"exit_code": None, "timed_out": True},
        {"stdout_truncated": True},
    ],
)
def test_failed_worker_keeps_diagnostics(backend, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(backend.runner, "run", lambda *a, **kw: result(**failure))
    value = backend.execute(tmp_path, "git_status", {})
    assert not value.success
    assert value.error_code == "SANDBOX_EXECUTION_FAILED"
    assert value.data["cleanup_status"] == "confirmed"
    assert value.data["output_complete"] is False
    for key, expected in failure.items():
        assert value.data[key] == expected


@pytest.mark.parametrize(
    "payload", ["not json", "[]", '{"success":"yes"}', '{"success":true,"data":[]}']
)
def test_invalid_protocol_is_a_tool_failure(backend, tmp_path, monkeypatch, payload):
    monkeypatch.setattr(backend.runner, "run", lambda *a, **kw: result(stdout=payload))
    value = backend.execute(tmp_path, "git_status", {})
    assert value.error_code == "SANDBOX_PROTOCOL_ERROR"
    assert value.data["stdout"] == payload


def test_failure_output_is_bounded_with_head_and_tail(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(
        backend.runner,
        "run",
        lambda *a, **kw: result(
            exit_code=1,
            stderr="start" + "x" * 50000 + "end",
        ),
    )
    data = backend.execute(tmp_path, "git_status", {}).data
    assert data["stderr"].startswith("start") and data["stderr"].endswith("end")
    assert len(data["stderr"]) < 17000 and data["stderr_truncated"]


def test_cleanup_failure_disables_backend(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(backend.runner, "run", lambda *a, **kw: result())
    monkeypatch.setattr(
        "sandbox.docker.subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1,
            stderr=b"daemon unavailable",
        ),
    )
    with pytest.raises(OSError, match="清理失败"):
        backend.execute(tmp_path, "git_status", {})
    with pytest.raises(OSError, match="之前"):
        backend.execute(tmp_path, "git_status", {})
    assert not backend.healthy


def test_smoke_help_does_not_require_docker():
    completed = subprocess.run(
        [sys.executable, "-m", "sandbox.docker_smoke", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--image" in completed.stdout


def test_smoke_fails_before_creating_session_without_docker(monkeypatch):
    from sandbox import docker_smoke

    monkeypatch.setattr("sandbox.environment.shutil.which", lambda _: None)
    monkeypatch.setattr(docker_smoke, "SandboxSession", lambda *a, **kw: pytest.fail("fallback"))
    with pytest.raises(ValueError, match="Docker"):
        docker_smoke.run_smoke()


@pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_DOCKER_TESTS") != "1",
    reason="Requires Docker and a built standard sandbox image",
)
def test_real_outer_timeout_removes_container_and_recovers(tmp_path, monkeypatch):
    from sandbox import SandboxSession

    session = SandboxSession(tmp_path)
    backend = session.backend
    original = backend.runner.run
    names = []

    def short_run(command, **options):
        names.append(command[command.index("--name") + 1])
        return original(command, **(options | {"timeout_seconds": 3}))

    try:
        monkeypatch.setattr(backend.runner, "run", short_run)
        outcome = backend.execute(
            session.workspace,
            "run_python",
            {
                "code": "import time; time.sleep(60)",
                "timeout_seconds": 30,
            },
        )
        assert outcome.error_code == "SANDBOX_EXECUTION_FAILED", outcome
        assert outcome.data["timed_out"], outcome
        assert outcome.data["cleanup_status"] == "confirmed", outcome
        inspected = subprocess.run(
            [backend.executable, "container", "inspect", names[0]],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert inspected.returncode != 0 and "No such" in inspected.stderr, inspected
        monkeypatch.setattr(backend.runner, "run", original)
        recovered = backend.execute(session.workspace, "run_python", {"code": "print('ok')"})
        assert recovered.success and recovered.data["exit_code"] == 0, recovered
    finally:
        if backend.healthy:
            shutil.rmtree(session.directory)
