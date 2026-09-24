import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent.skills import SkillRegistry
from sandbox import SandboxPolicy, SandboxSession, compute_probe, operator_smoke
from sandbox.docker import DockerBackend
from tools import create_default_tools
from tools._internal.lsp_config import default_lsp_registry
from tools._internal.process_runner import ProcessRunner


def test_cuda_routes_and_python_frameworks():
    registry = default_lsp_registry()
    for filename in ("kernel.cu", "kernel.cuh"):
        config = registry.select(filename)
        assert (config.language_id, config.server_id) == ("cuda", "clangd")
    for filename in ("triton_kernel.py", "torch_reference.py"):
        assert registry.select(filename).language_id == "python"


def test_profiles_keep_gpu_opt_in_and_budget_consistent(tmp_path):
    standard = SandboxPolicy.for_profile("standard")
    assert standard.gpus is None and standard.image == "repo-agent-sandbox:v1"
    gpu = SandboxPolicy.for_profile("cuda", gpus="0")
    assert gpu.gpus == "0" and gpu.image == "repo-agent-sandbox:v1"
    assert gpu.timeout > gpu.command_timeout_seconds == gpu.python_timeout_seconds == 900
    tools = {
        t.definition.name: t
        for t in create_default_tools(
            tmp_path,
            isolated_execution=True,
            command_timeout_seconds=gpu.command_timeout_seconds,
            python_timeout_seconds=gpu.python_timeout_seconds,
        )
    }
    for name in ("run_command", "run_python"):
        assert tools[name].definition.parameters["properties"]["timeout_seconds"]["maximum"] == 900
    with pytest.raises(ValueError):
        SandboxPolicy.for_profile("standard", gpus="all")
    with pytest.raises(ValueError):
        SandboxPolicy.for_profile("cuda", gpus="0 --privileged")


def test_gpu_flags_and_worker_limits_are_host_owned(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="sha256:gpu",
            stderr=b"",
        ),
    )
    backend = DockerBackend(SandboxPolicy.for_profile("cuda", gpus="0"))

    def run(command, **kwargs):
        calls.append(command)
        mounts = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "--mount"]
        request_mount = next(m for m in mounts if "dst=/request.json" in m)
        from pathlib import Path

        payload = json.loads(Path(request_mount.split("src=", 1)[1].split(",")[0]).read_text())
        assert payload["tool_limits"] == {
            "command_timeout_seconds": 900,
            "python_timeout_seconds": 900,
        }
        return SimpleNamespace(
            timed_out=False,
            exit_code=0,
            stdout_truncated=False,
            stdout='{"success": true, "data": {}}',
        )

    monkeypatch.setattr(backend.runner, "run", run)
    assert backend.execute(tmp_path, "run_command", {"command": ["nvcc", "--version"]}).success
    command = calls[0]
    assert command[command.index("--gpus") + 1] == "device=0"
    for flag in (
        "--read-only",
        "--network=none",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
    ):
        assert flag in command
    assert "--privileged" not in command
    assert command[command.index("--tmpfs") + 1].endswith("size=2g,mode=1777")


def test_compute_variables_reach_subprocess_without_credentials(monkeypatch):
    monkeypatch.setenv("CUDA_HOME", "/usr/local/cuda")
    monkeypatch.setenv("TRITON_CACHE_DIR", "/tmp/triton")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-for-process")
    environment = ProcessRunner().base_env
    assert environment["CUDA_HOME"] == "/usr/local/cuda"
    assert environment["TRITON_CACHE_DIR"] == "/tmp/triton"
    assert "DEEPSEEK_API_KEY" not in environment


def test_probe_reports_missing_dependencies(monkeypatch):
    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(compute_probe.importlib, "import_module", missing)
    monkeypatch.setattr(compute_probe.shutil, "which", lambda _: None)
    result = compute_probe.collect()
    assert not result["operator_environment_ready"]
    assert not result["cuda_available"]
    assert result["errors"] == {"torch": "ImportError", "triton": "ImportError"}


def test_probe_reports_devices_and_toolchain(monkeypatch):
    cuda = SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: 1,
        get_device_properties=lambda _: SimpleNamespace(
            name="test GPU", major=8, minor=0, total_memory=1024
        ),
    )
    torch = SimpleNamespace(__version__="2.7.1", version=SimpleNamespace(cuda="12.8"), cuda=cuda)
    monkeypatch.setattr(
        compute_probe.importlib,
        "import_module",
        lambda name: torch if name == "torch" else SimpleNamespace(__version__="3.3.1"),
    )
    monkeypatch.setattr(compute_probe.shutil, "which", lambda _: "/cuda/nvcc")
    monkeypatch.setattr(
        compute_probe.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="CUDA 12.8"),
    )
    result = compute_probe.collect()
    assert result["operator_environment_ready"]
    assert result["devices"][0]["compute_capability"] == [8, 0]
    assert result["torch_status"] == result["triton_status"] == result["nvcc_status"] == "available"


def test_probe_distinguishes_missing_package_from_broken_dependency(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError("missing dependency", name="torch" if name == "torch" else "dependency")

    monkeypatch.setattr(compute_probe.importlib, "import_module", missing)
    monkeypatch.setattr(compute_probe.shutil, "which", lambda _: None)
    result = compute_probe.collect()
    assert result["torch_status"] == "missing"
    assert result["triton_status"] == "unavailable"
    assert result["nvcc_status"] == "missing"


def test_gpu_smoke_refuses_to_pass_without_gpu(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["operator_smoke"])
    monkeypatch.setattr(operator_smoke, "collect", lambda: {"operator_environment_ready": False})
    monkeypatch.setattr(operator_smoke, "run_checks", lambda **kw: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as error:
        operator_smoke.main()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_builtin_operator_skill_loads(tmp_path):
    registry = SkillRegistry(tmp_path)
    skill = registry.get("gpu-kernel-development")
    assert skill.base_path is None
    assert registry.explicit("$gpu-kernel-development 写一个加法算子") == [skill]


@pytest.mark.parametrize(
    "args",
    [
        ["--sandbox", "local", "--sandbox-profile", "cuda"],
        ["--sandbox-profile", "standard", "--sandbox-gpus", "all"],
        ["--sandbox-profile", "cuda", "--sandbox-gpus", "invalid"],
    ],
)
def test_cli_rejects_invalid_gpu_configuration(args):
    result = subprocess.run(
        [sys.executable, "-m", "cli.main", *args], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 2


@pytest.mark.skipif(
    os.getenv("RUN_CUDA_DOCKER_TESTS") != "1",
    reason="Requires Linux NVIDIA GPU and built CUDA sandbox image",
)
def test_real_cuda_triton_torch_operator_smoke(tmp_path):
    (tmp_path / "example.cu").write_text(
        "#include <cuda_runtime.h>\n__global__ void example(float* out) { out[threadIdx.x] = 1; }\n"
    )
    session = SandboxSession(tmp_path, policy=SandboxPolicy.for_profile("cuda"))
    try:
        tools = {tool.definition.name: tool for tool in session.tools()}
        symbols = tools["get_symbols"].execute({"path": "example.cu"})
        assert symbols.success, symbols
        assert any(item["name"] == "example" for item in symbols.data["symbols"])
        result = tools["run_command"].execute(
            {
                "command": ["python", "-I", "-m", "sandbox.operator_smoke"],
                "timeout_seconds": 900,
                "check_id": "gpu-environment-smoke",
            }
        )
        assert result.success, result
        assert result.data["exit_code"] == 0, result.data
        assert '"status": "passed"' in result.data["stdout"]
    finally:
        shutil.rmtree(session.directory)
