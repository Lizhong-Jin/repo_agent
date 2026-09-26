import json
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.docker import DockerBackend
from sandbox.policy import SandboxPolicy
from sandbox.session import SandboxedTool
from sandbox.writeback import WritebackGuard
from tools import ExecutionKind, GetExecutionEnvironmentTool, create_default_tools
from tools._internal.process_runner import ProcessResult, ProcessStartError


def process_result(stdout="v1.2.3", **overrides):
    values = dict(
        exit_code=0,
        stdout=stdout,
        stderr="",
        timed_out=False,
        cleanup_error=None,
        duration_ms=1,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    values.update(overrides)
    return ProcessResult(**values)


def test_local_report_never_spawns_or_exposes_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_API_KEY", "SYNTHETIC_SECRET")
    tool = GetExecutionEnvironmentTool(tmp_path)
    monkeypatch.setattr(tool.runner, "run", lambda *a, **kw: pytest.fail("local probe spawned"))
    result = tool.execute({"sections": list(tool.SECTIONS)})
    assert result.success
    execution = result.data["execution"]
    assert execution["mode"] == "local"
    assert execution["command_execution_allowed"] is False
    assert execution["writeback_mode"] == "direct"
    assert execution["resources"] is None
    assert execution["workspace_root"] == str(tmp_path.resolve())
    assert result.data["system"]["python_version"]
    assert result.data["runtimes"]["python"]["status"] == "available"
    assert result.data["runtimes"]["node"]["status"] == "unknown"
    assert result.data["gpu"] == {"status": "unknown", "reason": "local_probes_disabled"}
    assert "SYNTHETIC_SECRET" not in json.dumps(result.data)
    assert "PRIVATE_API_KEY" not in json.dumps(result.data)


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        {"sections": []},
        {"sections": "gpu"},
        {"sections": None},
        {"sections": [None]},
        {"sections": [{}]},
        {"sections": ["system", "system"]},
        {"sections": ["secret"]},
        {"command": ["env"]},
        {"execution_allowed": True},
        {"sections": ["system"], "execution_context": {"mode": "docker"}},
    ],
)
def test_bad_arguments_rejected_before_probing(tmp_path, monkeypatch, arguments):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(tool.runner, "run", lambda *a, **kw: pytest.fail("invalid probe spawned"))
    assert tool.execute(arguments).error_code == "INVALID_ARGUMENTS"


def test_registration_defaults_and_selective_sections(tmp_path, monkeypatch):
    for isolated in [False, True]:
        tools = {
            t.definition.name: t
            for t in create_default_tools(tmp_path, isolated_execution=isolated)
        }
        tool = tools["get_execution_environment"]
        monkeypatch.setattr(tool, "_gpu", lambda *a: pytest.fail("GPU probe must be opt-in"))
        monkeypatch.setattr(tool, "_runtimes", lambda *a: {"python": {"status": "available"}})
        assert set(tool.execute({}).data) == {"execution", "system", "runtimes"}
        assert set(tool.execute({"sections": ["system"]}).data) == {"system"}
        assert ("run_command" in tools) == isolated
        schema = tool.definition.parameters
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {"sections"}


def test_trusted_context_and_configured_limits_are_copied(tmp_path):
    context = {"mode": "docker", "resources": {"cpu_limit": "1", "memory_limit": "512m"}}
    tool = GetExecutionEnvironmentTool(
        tmp_path,
        execution_allowed=True,
        execution_context=context,
        command_timeout_seconds=900,
        python_timeout_seconds=900,
    )
    context["resources"]["cpu_limit"] = "99"
    first = tool.execute({"sections": ["execution"]}).data["execution"]
    assert first["resources"]["cpu_limit"] == "1"
    assert first["tool_limits"]["command_max_timeout_seconds"] == 900
    assert first["tool_limits"]["python_max_timeout_seconds"] == 900
    first["resources"].clear()
    assert tool.execute({"sections": ["execution"]}).data["execution"]["resources"]
    with pytest.raises(ValueError):
        GetExecutionEnvironmentTool(tmp_path, execution_context=context)


@pytest.mark.parametrize(
    "option,value",
    [
        ("execution_allowed", 1),
        ("execution_context", []),
        ("command_timeout_seconds", 0),
        ("python_timeout_seconds", True),
        ("probe_timeout_seconds", -1),
    ],
)
def test_invalid_constructor_settings(tmp_path, option, value):
    with pytest.raises(ValueError):
        GetExecutionEnvironmentTool(tmp_path, **{option: value})


def test_runtime_missing_broken_and_timed_out_are_independent(tmp_path, monkeypatch):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(
        tool,
        "RUNTIME_COMMANDS",
        {
            "node": ("node", "--version"),
            "git": ("git", "--version"),
            "go": ("go", "version"),
            "clang": ("clang", "--version"),
        },
    )
    monkeypatch.setattr(
        shutil, "which", lambda name, **kw: None if name == "node" else "/bin/" + name
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0].endswith("go"):
            return process_result(exit_code=1, stderr="PRIVATE_ERROR")
        if argv[0].endswith("clang"):
            return process_result(timed_out=True)
        return process_result("git version 2.0\nextra")

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute({"sections": ["runtimes"]})
    assert result.success
    runtimes = result.data["runtimes"]
    assert runtimes["node"]["status"] == "missing"
    assert runtimes["git"]["version"] == "git version 2.0"
    assert runtimes["go"]["status"] == "unavailable"
    assert runtimes["clang"]["status"] == "unknown"
    assert "PRIVATE_ERROR" not in str(result)
    assert all(
        kw["cwd"] == tmp_path.resolve() and 0 < kw["timeout_seconds"] <= 3 for _, kw in calls
    )


@pytest.mark.parametrize(
    "result,expected",
    [
        (process_result(stdout_truncated=True), "unknown"),
        (process_result(stderr_truncated=True), "unknown"),
        (ProcessStartError(FileNotFoundError()), "missing"),
        (ProcessStartError(PermissionError()), "unavailable"),
    ],
)
def test_probe_error_statuses(tmp_path, monkeypatch, result, expected):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)

    def run(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tool.runner, "run", run)
    assert tool._probe(["fixed-command"], time.monotonic() + 60)["status"] == expected


def test_shared_probe_budget_and_cleanup_failure(tmp_path, monkeypatch):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(tool.runner, "run", lambda *a, **kw: pytest.fail("budget exhausted"))
    assert tool._probe(["fixed-command"], 0)["reason"] == "probe_budget_exhausted"
    monkeypatch.setattr(shutil, "which", lambda *a, **kw: "/bin/fake")
    calls = []

    def run(*a, **kw):
        calls.append(a)
        return process_result(cleanup_error="unconfirmed")

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute({"sections": ["execution", "runtimes", "gpu"]})
    assert len(calls) == 1
    assert not result.success and result.error_code == "PROBE_CLEANUP_FAILED"
    guard = WritebackGuard()
    guard.record(tool.definition.name, {}, result)
    assert guard.needs_review


@pytest.mark.parametrize(
    "cuda_available,torch_error,expected",
    [
        (True, False, "available"),
        (False, False, "unavailable"),
        (False, True, "unknown"),
    ],
)
def test_gpu_report_and_driver_probe(tmp_path, monkeypatch, cuda_available, torch_error, expected):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
    report = {
        "torch": "2.test",
        "triton": "3.test",
        "cuda_available": cuda_available,
        "devices": [{"name": "Test GPU", "compute_capability": [8, 0]}] if cuda_available else [],
        "errors": {"torch": "RuntimeError"} if torch_error else {},
        "torch_cuda_version": "12.test",
        "nvcc": "CUDA compiler 12.test",
    }
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return process_result(json.dumps(report) if "-I" in argv else "Test GPU, driver.test")

    monkeypatch.setattr(tool.runner, "run", run)
    monkeypatch.setattr(shutil, "which", lambda *a, **kw: "/bin/nvidia-smi")
    result = tool.execute({"sections": ["gpu"]})
    assert result.success and result.data["gpu"]["status"] == expected
    assert result.data["gpu"]["driver"]["status"] == "available"
    assert "-I" in commands[0] and commands[0][-1].endswith("sandbox/compute_probe.py")
    assert commands[1] == [
        "/bin/nvidia-smi",
        "--query-gpu=name,driver_version",
        "--format=csv,noheader",
    ]
    assert result.data["gpu"]["torch_cuda_version"] == "12.test"


@pytest.mark.parametrize("output", ["not JSON", "[]", "{}", '{"cuda_available": "false"}'])
def test_invalid_gpu_response_is_unknown(tmp_path, monkeypatch, output):
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True)
    monkeypatch.setattr(tool.runner, "run", lambda *a, **kw: process_result(output))
    result = tool.execute({"sections": ["gpu"]})
    assert result.success
    assert result.data["gpu"] == {"status": "unknown", "reason": "invalid_probe_response"}


def test_backend_sends_actual_policy_separately_from_model_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *a, **kw: "/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="sha256:fixed-image",
            stderr=b"",
        ),
    )
    backend = DockerBackend(SandboxPolicy.for_profile("cuda", gpus="0"))
    captured = []

    def run(argv, **kwargs):
        mount = next(arg for arg in argv if "dst=/request.json" in arg)
        payload = json.loads(Path(mount.split("src=", 1)[1].split(",")[0]).read_text())
        captured.append(payload)
        context = payload["execution_context"]
        assert context["image_id"] == "sha256:fixed-image"
        assert context["network"] == "disabled"
        assert context["resources"]["cpu_limit"] == "4"
        assert context["resources"]["memory_limit"] == "8g"
        assert context["resources"]["gpu_selection"] == "0"
        assert not context["persistence"]["processes_across_calls"]
        assert argv[argv.index("--cpus") + 1] == context["resources"]["cpu_limit"]
        return process_result(json.dumps({"success": True, "data": {"execution": context}}))

    monkeypatch.setattr(backend.runner, "run", run)
    args = {"sections": ["execution"]}
    assert backend.execute(tmp_path, "get_execution_environment", args).success
    assert captured[0]["arguments"] == args
    assert "execution_context" not in args


def test_session_adds_writeback_without_mutating_backend_result(tmp_path):
    original = GetExecutionEnvironmentTool(tmp_path).execute({"sections": ["execution"]})
    snapshot = deepcopy(original.data)
    session = SimpleNamespace(
        workspace=tmp_path,
        guard=WritebackGuard(),
        backend=SimpleNamespace(execute=lambda *args: original),
    )
    proxy = SandboxedTool(
        GetExecutionEnvironmentTool(tmp_path).definition, session,
        execution_kind=ExecutionKind.SANDBOXED_PROCESS, writeback_mode="manual"
    )
    assert proxy.execute({}).data["execution"]["writeback_mode"] == "manual"
    assert original.data == snapshot
    assert not session.guard.pending


def test_worker_passes_host_context_to_factory(monkeypatch, capsys):
    from sandbox import worker
    from tools import ToolResult

    request = {
        "name": "get_execution_environment",
        "arguments": {"sections": ["execution"]},
        "execution_context": {"mode": "docker", "network": "disabled"},
        "tool_limits": {"command_timeout_seconds": 900, "python_timeout_seconds": 900},
    }
    monkeypatch.setattr(
        worker, "Path", lambda path: SimpleNamespace(read_text=lambda: json.dumps(request))
    )

    def factory(root, **kwargs):
        assert root == "/workspace"
        assert kwargs["isolated_execution"] is True
        assert kwargs["execution_context"] == request["execution_context"]
        assert kwargs["command_timeout_seconds"] == 900

        def execute(arguments):
            assert arguments == request["arguments"]
            return ToolResult(True, {"execution": kwargs["execution_context"]})

        return [SimpleNamespace(definition=SimpleNamespace(name=request["name"]),
                                execution_kind=ExecutionKind.SANDBOXED_PROCESS, execute=execute)]

    monkeypatch.setattr(worker, "create_default_tools", factory)
    worker.main()
    assert json.loads(capsys.readouterr().out)["data"]["execution"]["mode"] == "docker"
