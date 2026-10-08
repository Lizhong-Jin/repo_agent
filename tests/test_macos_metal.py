"""Opt-in real Metal tests plus portable policy and fail-closed report checks."""

import json
import os
import platform
import sys
from types import SimpleNamespace

import pytest

from sandbox import macos_native, metal_check
from sandbox.macos_native import METAL_COMPUTE_RULES, seatbelt_profile


def test_metal_only_adds_two_scoped_permissions(tmp_path):
    arguments = (tmp_path / "workspace", tmp_path / "scratch", [tmp_path / "runtime"], [])
    standard = seatbelt_profile(*arguments)
    metal = seatbelt_profile(*arguments, metal=True)
    assert [line for line in metal.splitlines() if line not in standard.splitlines()] == list(
        METAL_COMPUTE_RULES
    )
    assert all(line in metal.splitlines() for line in standard.splitlines())
    assert "(allow iokit-open)" not in metal
    assert "(allow mach-lookup)" not in metal
    assert "IOSurface" not in metal and "file-issue-extension" not in metal
    assert macos_native.MacOSNativeBackend._metal_compute is False


def valid_report():
    return {
        "metal_kernel_verified": True,
        "runtime_shader_compilation": True,
        "isolation_verified": True,
        "elements": 256,
        "result_checksum": 132352,
        "device": "test device",
    }


def result_for(report, **overrides):
    return SimpleNamespace(
        **{
            "exit_code": 0,
            "timed_out": False,
            "stdout_truncated": False,
            "stdout": json.dumps(report),
            "stderr": "",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("metal_kernel_verified", False),
        ("metal_kernel_verified", 1),
        ("runtime_shader_compilation", False),
        ("isolation_verified", False),
        ("elements", 0),
        ("result_checksum", 0),
        ("device", ""),
        ("device", None),
    ],
)
def test_incomplete_or_incorrect_computation_is_rejected(key, value):
    report = {**valid_report(), key: value}
    with pytest.raises(RuntimeError, match="invalid report"):
        metal_check._checked_report(result_for(report), True)


@pytest.mark.parametrize(
    "fields,healthy",
    [
        ({"exit_code": 1}, True),
        ({"timed_out": True}, True),
        ({"stdout_truncated": True}, True),
        ({}, False),
    ],
)
def test_unsuccessful_process_cannot_claim_success(fields, healthy):
    with pytest.raises(RuntimeError, match="no unrestricted retry"):
        metal_check._checked_report(result_for(valid_report(), **fields), healthy)


@pytest.mark.parametrize("report", [None, [], {}, "not a report"])
def test_malformed_report_is_rejected(report):
    with pytest.raises(RuntimeError, match="invalid report"):
        metal_check._checked_report(result_for(report), True)


def test_probe_failure_returns_nonzero_json(monkeypatch, capsys):
    from sandbox import metal_probe

    def fail():
        raise RuntimeError("compile failed")

    monkeypatch.setattr(metal_probe, "probe", fail)
    assert metal_probe.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "metal_kernel_verified": False,
        "error": "compile failed",
    }


REAL_METAL = pytest.mark.skipif(
    sys.platform != "darwin"
    or platform.machine() != "arm64"
    or os.getenv("RUN_NATIVE_METAL_TESTS") != "1",
    reason="Requires Apple Silicon GPU outside an outer sandbox; RUN_NATIVE_METAL_TESTS=1",
)


@REAL_METAL
def test_real_metal_compute_and_isolation():
    report = metal_check.check()
    assert report["metal_kernel_verified"] is True
    assert report["isolation_verified"] is True
    assert report["result_checksum"] == 132352


@REAL_METAL
@pytest.mark.parametrize(
    "rules,error",
    [
        ((), "create Metal device failed"),
        ((METAL_COMPUTE_RULES[0],), "compile Metal source failed"),
        ((METAL_COMPUTE_RULES[1],), "create Metal device failed"),
    ],
    ids=["no-gpu-permissions", "no-compiler-permission", "no-device-permission"],
)
def test_removing_either_permission_prevents_compute(tmp_path, monkeypatch, rules, error):
    monkeypatch.setattr(macos_native, "METAL_COMPUTE_RULES", rules)
    with pytest.raises(ValueError, match=error):
        macos_native.MacOSNativeBackend(tmp_path, profile="metal")


@pytest.mark.parametrize(
    "profile,machine,enabled",
    [
        ("auto", "arm64", True),
        ("metal", "arm64", True),
        ("standard", "arm64", False),
        ("auto", "x86_64", False),
    ],
)
def test_profile_selects_metal_only_on_apple_silicon(monkeypatch, profile, machine, enabled):
    from pathlib import Path

    monkeypatch.setattr(macos_native.sys, "platform", "darwin")
    monkeypatch.setattr(macos_native.platform, "machine", lambda: machine)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr("host_support.rust_filesystem.select_directory_backend", lambda: None)
    backend = object.__new__(macos_native.MacOSNativeBackend)
    backend.requested_profile = profile
    backend._platform_setup()
    assert backend._metal_compute is enabled
    assert backend.gpu_probe is None
    if enabled:
        assert backend.python_timeout_seconds == 900


@pytest.mark.parametrize("profile", ["auto", "metal"])
@pytest.mark.parametrize("failure", ["timeout", "incorrect", "truncated", "unhealthy", "invalid"])
def test_metal_startup_failure_never_downgrades(profile, failure):
    backend = object.__new__(macos_native.MacOSNativeBackend)
    backend.requested_profile = profile
    backend._metal_compute = True
    backend.healthy = failure != "unhealthy"
    result = result_for(valid_report())
    if failure == "timeout":
        result.timed_out = True
    elif failure == "truncated":
        result.stdout_truncated = True
    elif failure == "invalid":
        result.stdout = "not json"
    elif failure == "incorrect":
        result.stdout = json.dumps({**valid_report(), "result_checksum": 0})
    with pytest.raises(ValueError, match="Metal 自检失败"):
        backend._validate_metal_preflight(result)
    assert backend._metal_compute  # no permission removal / silent CPU downgrade
    assert backend.gpu_probe is None


@pytest.mark.parametrize(
    "mode,host,profile,gpus,accepted",
    [
        ("native", "darwin", "metal", None, True),
        ("native", "darwin", "auto", None, True),
        ("native", "darwin", "cuda", None, False),
        ("native", "darwin", "metal", "all", False),
        ("native", "darwin", "auto", "0", False),
        ("native", "linux", "metal", None, False),
        ("native", "linux", "cuda", "0", True),
        ("docker", "darwin", "metal", None, False),
        ("docker", "linux", "metal", None, False),
        ("docker", "darwin", "cuda", "all", True),
        ("local", "darwin", "metal", None, False),
    ],
)
def test_cli_validates_backend_specific_gpu_profiles(
    monkeypatch, mode, host, profile, gpus, accepted
):
    from cli import arguments

    monkeypatch.setattr(arguments, "sys", SimpleNamespace(platform=host))
    parser = arguments.create_parser()
    argv = ["--sandbox", mode, "--sandbox-profile", profile]
    if gpus is not None:
        argv += ["--sandbox-gpus", gpus]
    args = parser.parse_args(argv)
    if accepted:
        arguments.validate_execution_options(parser, args)
    else:
        with pytest.raises(SystemExit):
            arguments.validate_execution_options(parser, args)


@REAL_METAL
@pytest.mark.parametrize("profile", ["auto", "metal", "standard"])
def test_real_profiles_tools_and_environment(tmp_path, profile):
    backend = macos_native.MacOSNativeBackend(tmp_path, profile=profile)
    try:
        enabled = profile != "standard"
        access = backend.execution_context()["gpu_access"]
        assert access["enabled"] is enabled
        assert access["profile"] == ("metal" if enabled else "standard")
        result = backend.execute(
            tmp_path,
            "run_python",
            {
                "code": 'import subprocess, sys; r = subprocess.run([sys.executable, "-I", '
                + repr(str(backend.runtime / "sandbox/metal_probe.py"))
                + "]); sys.exit(r.returncode)",
                "timeout_seconds": 90 if enabled else 30,
            },
        )
        assert result.data["exit_code"] == (0 if enabled else 1), result
        report = backend.execute(
            tmp_path,
            "get_execution_environment",
            {
                "sections": ["execution", "gpu"],
            },
        )
        assert report.success, report
        assert report.data["gpu"]["metal"]["kernel_verified"] is enabled
        assert report.data["gpu"]["status"] == ("available" if enabled else "unavailable")
        assert report.data["execution"]["gpu_access"]["enabled"] is enabled
    finally:
        backend.close()


@REAL_METAL
@pytest.mark.skipif(not os.getenv("NATIVE_TEST_MPS_PYTHON"), reason="Optional project PyTorch env")
def test_real_project_python_mps(tmp_path):
    backend = macos_native.MacOSNativeBackend(
        tmp_path,
        profile="metal",
        project_python=os.environ["NATIVE_TEST_MPS_PYTHON"],
    )
    try:
        report = backend.execute(tmp_path, "get_execution_environment", {"sections": ["gpu"]})
        assert report.success, report
        assert report.data["gpu"]["mps"]["kernel_verified"] is True, report
        result = backend.execute(
            tmp_path,
            "run_python",
            {
                "code": 'import torch; x = torch.ones(8, device="mps"); '
                "y = x + x; torch.mps.synchronize(); "
                'assert y.device.type == "mps"; assert y.cpu().tolist() == [2.] * 8',
                "timeout_seconds": 90,
            },
        )
        assert result.data["exit_code"] == 0, result
    finally:
        backend.close()
