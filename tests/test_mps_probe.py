"""MPS capability must reflect a verified operation, independent of CUDA packages."""

import json
from types import SimpleNamespace

import pytest

from sandbox import compute_probe
from tools import GetExecutionEnvironmentTool
from tools._internal.process_runner import ProcessResult


class Tensor:
    device = SimpleNamespace(type="mps")

    def __init__(self, values):
        self.values = values

    def __matmul__(self, other):
        return Tensor([[7.0, 10.0], [15.0, 22.0]])

    def cpu(self):
        return self

    def tolist(self):
        return self.values


def torch_module():
    return SimpleNamespace(
        backends=SimpleNamespace(
            mps=SimpleNamespace(is_built=lambda: True, is_available=lambda: True)
        ),
        tensor=lambda values, **kw: Tensor(values),
        float32="float32",
        mps=SimpleNamespace(synchronize=lambda: None),
    )


def test_mps_probe_requires_real_device_synchronization_and_correct_result(monkeypatch):
    torch = torch_module()
    calls = []
    torch.mps.synchronize = lambda: calls.append("synchronize")
    assert compute_probe.probe_mps(torch)["kernel_verified"] is True
    assert calls == ["synchronize"]
    monkeypatch.setattr(Tensor, "__matmul__", lambda *a: Tensor([[0.0]]))
    report = compute_probe.probe_mps(torch)
    assert report["status"] == "unavailable"
    assert report["kernel_verified"] is False


def test_cpu_fallback_is_not_gpu_success(monkeypatch):
    monkeypatch.setattr(Tensor, "device", SimpleNamespace(type="cpu"))
    assert not compute_probe.probe_mps(torch_module())["kernel_verified"]


def test_unavailable_mps_never_allocates_tensor():
    torch = torch_module()
    torch.backends.mps.is_available = lambda: False
    torch.tensor = lambda *a, **kw: pytest.fail("allocated")
    report = compute_probe.probe_mps(torch)
    assert report["built"] is True and report["available"] is False
    assert not report["kernel_verified"]


@pytest.mark.parametrize("mps_status", ["missing", "unavailable", "available"])
def test_metal_status_is_independent_of_project_pytorch(tmp_path, monkeypatch, mps_status):
    context = {
        "platform": "macos",
        "gpu_access": {
            "enabled": True,
            "startup_probe": {
                "metal_kernel_verified": True,
                "device": "Apple GPU",
            },
        },
        "python_environments": {"project": "/project/python"},
    }
    tool = GetExecutionEnvironmentTool(tmp_path, execution_allowed=True, execution_context=context)
    report = {
        "cuda_available": False,
        "devices": [],
        "errors": {"triton": "ModuleNotFoundError"},
        "mps": {"status": mps_status, "kernel_verified": mps_status == "available"},
    }
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        return ProcessResult(0, json.dumps(report), "", False, None, 1, False, False)

    monkeypatch.setattr(tool.runner, "run", run)
    result = tool.execute({"sections": ["gpu"]})
    assert result.data["gpu"]["status"] == "available"
    assert result.data["gpu"]["metal"]["kernel_verified"] is True
    assert result.data["gpu"]["mps"]["status"] == mps_status
    assert len(commands) == 1 and commands[0][0] == "/project/python"


def test_mps_check_flag_does_not_require_cuda(monkeypatch, capsys):
    monkeypatch.setattr(
        compute_probe,
        "collect",
        lambda: {
            "operator_environment_ready": False,
            "mps": {"kernel_verified": True},
        },
    )
    monkeypatch.setattr("sys.argv", ["probe", "--require-mps"])
    compute_probe.main()
    assert json.loads(capsys.readouterr().out)["mps"]["kernel_verified"]
    monkeypatch.setattr(compute_probe, "collect", lambda: {"mps": {"kernel_verified": False}})
    with pytest.raises(SystemExit):
        compute_probe.main()
