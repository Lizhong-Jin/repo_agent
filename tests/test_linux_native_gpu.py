"""Device grants, fail-closed CUDA startup and opt-in hardware regression tests."""

import ctypes
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox import linux_gpu
from sandbox.linux_gpu import DeviceNode, NativeGPU
from sandbox.linux_native import LinuxNativeBackend
from sandbox.native import NativeBackend

UUID0 = "GPU-11111111-1111-1111-1111-111111111111"
UUID1 = "GPU-22222222-2222-2222-2222-222222222222"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """Synthetic host layout; character-device checks have their own real tests."""

    def path(value):
        return tmp_path / str(value).lstrip("/")

    def create(value, directory=False):
        target = path(value)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir() if directory else target.touch()
        return target

    monkeypatch.setattr(linux_gpu, "Path", path)
    monkeypatch.setattr(DeviceNode, "read", classmethod(lambda cls, p: cls(p, (1, 2, 3))))
    return SimpleNamespace(path=path, create=create)


def nvidia_host(host):
    for name in ("nvidiactl", "nvidia-uvm", "nvidia0", "nvidia7", "nvidia-modeset"):
        host.create("/dev/" + name)
    host.create("/proc/driver/nvidia", directory=True)


def test_explicit_nvidia_grant_excludes_unrelated_devices(host):
    nvidia_host(host)
    gpu = NativeGPU.discover("all")
    assert {p.path.name for p in gpu.devices} == {"nvidiactl", "nvidia-uvm", "nvidia0", "nvidia7"}
    assert gpu.read_paths == (host.path("/proc/driver/nvidia"),)
    assert "CUDA_VISIBLE_DEVICES" not in gpu.environment(host.path("scratch"))


@pytest.mark.parametrize("selection", ["1", UUID1])
def test_index_resolves_to_minor_and_uuid_not_index_filename(host, selection):
    nvidia_host(host)
    gpu = NativeGPU.discover(selection).select(f"0, 0, {UUID0}\n1, 7, {UUID1}\n")
    assert {node.path.name for node in gpu.devices} == {"nvidiactl", "nvidia-uvm", "nvidia7"}
    assert gpu.environment(host.path("scratch"))["CUDA_VISIBLE_DEVICES"] == UUID1


@pytest.mark.parametrize(
    "inventory",
    ["", "broken", f"0, 0, {UUID0}", f"1, 99, {UUID1}", f"1, 7, {UUID1}\n1, 7, {UUID1}"],
)
def test_invalid_missing_or_ambiguous_selection_fails(host, inventory):
    nvidia_host(host)
    with pytest.raises(ValueError):
        NativeGPU.discover("1").select(inventory)


def test_missing_gpu_does_not_fall_back(host):
    with pytest.raises(ValueError, match="未找到 NVIDIA"):
        NativeGPU.discover("all")
    with pytest.raises(ValueError, match="gpus must be"):
        NativeGPU.discover("0 --privileged")


def test_auto_uses_standard_without_nvidia_including_non_cuda_wsl(host):
    assert NativeGPU.detect() is None
    host.create("/dev/dxg")
    assert NativeGPU.detect() is None


@pytest.mark.parametrize("kind", ["nvidia", "wsl2"])
def test_auto_detects_nvidia_and_wsl_cuda(host, kind):
    if kind == "nvidia":
        nvidia_host(host)
    else:
        host.create("/dev/dxg")
        host.create("/usr/lib/wsl/lib/libcuda.so.1")
    gpu = NativeGPU.detect()
    assert gpu.kind == kind and gpu.requested == "all"


def test_auto_does_not_hide_broken_detected_driver(host):
    host.create("/dev/nvidia0")
    with pytest.raises(ValueError, match="缺少 NVIDIA 驱动信息"):
        NativeGPU.detect()
    host.path("/dev/nvidia0").unlink()
    host.create("/dev/dxg")
    cuda = host.path("/usr/lib/wsl/lib/libcuda.so.1")
    cuda.parent.mkdir(parents=True)
    cuda.symlink_to("missing-driver")
    with pytest.raises(ValueError, match="Windows NVIDIA"):
        NativeGPU.detect()


def test_wsl_only_all_and_requires_driver(host):
    host.create("/dev/dxg")
    with pytest.raises(ValueError, match="仅支持"):
        NativeGPU.discover("0")
    with pytest.raises(ValueError, match="Windows NVIDIA"):
        NativeGPU.discover("all")
    host.create("/usr/lib/wsl/lib/libcuda.so.1")
    gpu = NativeGPU.discover("all")
    assert gpu.kind == "wsl2"
    assert [node.path.name for node in gpu.devices] == ["dxg"]
    assert gpu.environment(host.path("scratch"))["LD_LIBRARY_PATH"] == "/usr/lib/wsl/lib"


def test_cuda_toolkit_resolves_system_alias_and_cache_is_private(host, monkeypatch):
    nvidia_host(host)
    host.create("/usr/local/cuda-12/bin/nvcc")
    host.path("/usr/local/cuda").symlink_to("cuda-12")
    monkeypatch.setenv("CUDA_HOME", "/home/private-toolkit")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/private-libraries")
    gpu = NativeGPU.discover("all")
    scratch = host.path("scratch")
    env = gpu.environment(scratch)
    assert gpu.cuda_home == host.path("/usr/local/cuda-12")
    assert env["CUDA_HOME"] == str(gpu.cuda_home)
    assert env["CUDA_CACHE_PATH"] == str(scratch / "cuda-cache")
    assert "/home/private" not in str(env)


def test_device_files_symlinks_missing_and_replacement_fail_closed(tmp_path):
    normal = tmp_path / "nvidia0"
    normal.touch()
    alias = tmp_path / "alias"
    alias.symlink_to("/dev/null")
    for path in (normal, alias, tmp_path / "missing"):
        with pytest.raises(ValueError):
            DeviceNode.read(path)
    node = DeviceNode.read(Path("/dev/null"))
    node.verify()
    with pytest.raises(ValueError, match="发生变化"):
        replace(node, identity=(0, 0, 0)).verify()


@pytest.fixture
def backend(tmp_path):
    instance = object.__new__(LinuxNativeBackend)
    instance.workspace = tmp_path / "workspace"
    instance.workspace.mkdir()
    instance.directory = tmp_path / "control"
    instance.directory.mkdir()
    instance.runtime = instance.directory / "runtime"
    instance.runtime.mkdir()
    instance.python = Path(sys.executable)
    instance.protected_paths = ()
    instance.read_paths = (instance.runtime,)
    instance.executable = Path("/usr/bin/bwrap")
    instance.healthy = True
    return instance


def test_device_mounts_do_not_replace_private_dev_or_relax_network(backend, tmp_path):
    backend.gpu = NativeGPU("nvidia", "all", (DeviceNode.read(Path("/dev/null")),))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    args = backend._sandbox_command(["true"], backend.directory, scratch, backend.read_paths)
    assert args[args.index("--dev") + 1] == "/dev"
    assert args[args.index("--dev-bind") + 1 : args.index("--dev-bind") + 3] == [
        "/dev/null",
        "/dev/null",
    ]
    assert "--unshare-net" in args and "--cap-drop" in args
    assert str(backend.runtime / "sandbox/linux_exec.py") in args
    assert "/run" not in args and "/sys" not in args
    (backend.directory / "hidden-dir").chmod(0o700)


def test_cpu_native_has_no_gpu_mounts_environment_or_probe(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "all")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    args = backend._sandbox_command(["true"], backend.directory, scratch, backend.read_paths)
    assert "--dev-bind" not in args
    assert "CUDA_VISIBLE_DEVICES" not in backend._environment(scratch)
    assert "LD_LIBRARY_PATH" not in backend._environment(scratch)
    assert backend.execution_context()["gpu_access"]["enabled"] is False
    assert backend._tool_limits() == {"command_timeout_seconds": 120, "python_timeout_seconds": 30}
    (backend.directory / "hidden-dir").chmod(0o700)


def result(stdout, **kwargs):
    return SimpleNamespace(
        stdout=stdout,
        stderr="driver diagnostic",
        exit_code=0,
        timed_out=False,
        stdout_truncated=False,
        **kwargs,
    )


def startup_report(*, gpu=None):
    report = {
        "isolation": {
            "exit_code": 0,
            "timed_out": False,
            "stdout": "native-ok\n",
            "stderr": "",
            "duration_ms": 1,
        }
    }
    if gpu is not None:
        report["gpu"] = {
            "exit_code": 0,
            "timed_out": False,
            "stdout": json.dumps(gpu),
            "stderr": "",
            "duration_ms": 2,
        }
    return report


@pytest.mark.parametrize(
    "failure", ["driver", "timeout", "cleanup", "protocol", "wrong_device", "cpu"]
)
def test_gpu_preflight_never_accepts_cpu_fallback_or_wrong_device(backend, monkeypatch, failure):
    backend.gpu = NativeGPU("nvidia", "0", (), visible_uuid=UUID0)
    report = {"cuda_kernel_verified": True, "devices": [UUID0]}
    outcome = result(json.dumps(report))
    if failure == "driver":
        outcome.exit_code = 1
    elif failure == "timeout":
        outcome.timed_out = True
    elif failure == "cleanup":
        backend.healthy = False
    elif failure == "protocol":
        outcome.stdout = "[]"
    elif failure == "wrong_device":
        outcome.stdout = json.dumps({**report, "devices": [UUID1]})
    else:
        outcome.stdout = json.dumps({"cuda_kernel_verified": False, "devices": []})
    check = {
        "stdout": outcome.stdout,
        "stderr": outcome.stderr,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
    }
    with pytest.raises(ValueError, match="CUDA 自检失败"):
        backend._validate_gpu_preflight(check, outcome)


def test_gpu_preflight_selects_then_checks_isolation_and_kernel(backend, monkeypatch):
    gpu = NativeGPU("nvidia", "1", ())
    selected = replace(gpu, visible_uuid=UUID1)
    backend.gpu = gpu
    calls = []

    def run(command, **kwargs):
        calls.append((command, backend.gpu))
        if len(calls) == 1:
            return result("inventory")
        return result(
            json.dumps(
                startup_report(
                    gpu={"cuda_kernel_verified": True, "devices": [UUID1]},
                )
            )
        )

    monkeypatch.setattr("sandbox.linux_native.shutil.which", lambda *a, **kw: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(NativeGPU, "select", lambda self, text: selected)
    monkeypatch.setattr(backend, "_run", run)
    backend._preflight()
    assert calls[0][1] == gpu
    assert len(calls) == 2 and calls[1][1] == selected
    assert "--gpu" in calls[1][0]
    assert backend.gpu_probe["devices"] == [UUID1]


@pytest.mark.parametrize(
    "flags,expected",
    [
        ([], ("auto", None)),
        (["--sandbox-profile", "auto"], ("auto", None)),
        (["--sandbox-profile", "standard"], ("standard", None)),
        (["--sandbox-profile", "cuda"], ("cuda", None)),
        (["--sandbox-gpus", "0"], ("auto", "0")),
    ],
)
def test_cli_passes_native_profile_and_gpu_selection(tmp_path, monkeypatch, flags, expected):
    from cli import main as cli

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(
        sys,
        "argv",
        ["repo-agent", "--sandbox", "native", "--root", str(tmp_path), "--model", "m", *flags],
    )
    monkeypatch.setattr(
        "cli.arguments.sys", SimpleNamespace(platform="linux", stdin=sys.stdin, stdout=sys.stdout)
    )
    captured = []

    def constructor(root, **kwargs):
        captured.append((kwargs["profile"], kwargs["gpus"]))
        raise ValueError("stop after backend selection")

    monkeypatch.setattr("cli.execution_environment.NativeBackend", constructor)
    with pytest.raises(SystemExit) as error:
        cli._main()
    assert error.value.code == 1
    assert captured == [expected]


@pytest.mark.parametrize(
    "flags", [["--sandbox-profile", "standard", "--sandbox-gpus", "all"], ["--sandbox-gpus", ""]]
)
def test_cli_rejects_conflicting_or_empty_selection(tmp_path, monkeypatch, flags):
    from cli import main as cli

    monkeypatch.setattr(sys, "argv", ["repo-agent", "--sandbox", "native", *flags])
    monkeypatch.setattr("cli.arguments.sys", SimpleNamespace(platform="linux"))
    with pytest.raises(SystemExit) as error:
        cli._main()
    assert error.value.code == 2


@pytest.mark.parametrize("options", [{"gpus": "all"}, {"profile": "cuda"}])
def test_macos_native_rejects_gpu_before_starting(tmp_path, options):
    backend = object.__new__(NativeBackend)
    with pytest.raises(ValueError, match="仅支持 Linux / WSL2 NVIDIA CUDA"):
        backend.__init__(tmp_path, **options)


@pytest.mark.parametrize(
    "profile,gpus,detected,enabled",
    [
        ("auto", None, False, False),
        ("auto", None, True, True),
        ("standard", None, True, False),
        ("cuda", None, False, True),
        ("auto", "0", False, True),
    ],
)
def test_linux_profile_controls_detection_and_grants(
    backend, monkeypatch, profile, gpus, detected, enabled
):
    monkeypatch.setattr("sandbox.linux_native.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr("sandbox.linux_native.shutil.which", lambda *a, **kw: "/usr/bin/bwrap")
    calls = []

    def detect():
        calls.append("auto")
        return NativeGPU("nvidia", "all", ()) if detected else None

    def discover(selection):
        calls.append(selection)
        return NativeGPU("nvidia", selection, ())

    monkeypatch.setattr(NativeGPU, "detect", detect)
    monkeypatch.setattr(NativeGPU, "discover", discover)
    backend.requested_profile, backend.requested_gpus = profile, gpus
    backend._platform_setup()
    assert (backend.gpu is not None) == enabled
    assert calls == (
        [] if profile == "standard" else [gpus or ("all" if profile == "cuda" else "auto")]
    )
    assert backend.python_timeout_seconds == (900 if enabled else 30)
    context = backend.execution_context()["gpu_access"]
    assert context["profile"] == ("cuda" if enabled else "standard")
    assert context["requested_profile"] == profile


@pytest.mark.parametrize("gpu_present", [False, True])
def test_default_constructor_probes_detected_gpu_and_propagates_failure(
    tmp_path, monkeypatch, gpu_present
):
    monkeypatch.setattr("sandbox.linux_native.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr("sandbox.linux_native.shutil.which", lambda *a, **kw: "/usr/bin/bwrap")
    monkeypatch.setattr(
        NativeGPU, "detect", lambda: NativeGPU("nvidia", "all", ()) if gpu_present else None
    )
    monkeypatch.setattr(LinuxNativeBackend, "_read_paths", lambda self: (self.runtime,))
    calls = []

    def run(self, command, **kwargs):
        calls.append(command)
        report = startup_report()
        if "--gpu" in command:
            report["gpu"] = {
                "exit_code": 1,
                "timed_out": False,
                "stdout": "",
                "stderr": "driver failed",
            }
        outcome = result(json.dumps(report))
        outcome.exit_code = 1 if gpu_present else 0
        return outcome

    monkeypatch.setattr(LinuxNativeBackend, "_run", run)
    if gpu_present:
        with pytest.raises(ValueError, match="CUDA 自检失败"):
            LinuxNativeBackend(tmp_path)
        assert len(calls) == 1  # No retry without GPU or unrestricted fallback.
    else:
        backend = LinuxNativeBackend(tmp_path, project_python=sys.executable)
        try:
            assert backend.gpu is None and len(calls) == 1
        finally:
            backend.close()


def test_gpu_setup_and_tool_limits_reach_definitions_and_worker(backend, monkeypatch):
    monkeypatch.setattr("sandbox.linux_native.sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr("sandbox.linux_native.shutil.which", lambda *a, **kw: "/usr/bin/bwrap")
    monkeypatch.setattr(NativeGPU, "discover", lambda selection: NativeGPU("nvidia", selection, ()))
    backend.requested_gpus = "all"
    backend._platform_setup()
    definitions = {tool.definition.name: tool.definition for tool in backend.tools()}
    assert definitions["run_python"].parameters["properties"]["timeout_seconds"]["maximum"] == 900
    assert definitions["run_command"].parameters["properties"]["timeout_seconds"]["maximum"] == 900
    captured = []

    def run(*args, **kwargs):
        captured.append(kwargs["request"])
        return SimpleNamespace(
            exit_code=0,
            timed_out=False,
            stdout_truncated=False,
            cleanup_error=None,
            stdout='{"success": true, "data": {}}',
        )

    monkeypatch.setattr(backend, "_run", run)
    assert backend.execute(backend.workspace, "get_execution_environment", {}).success
    assert captured[0]["tool_limits"] == {
        "command_timeout_seconds": 900,
        "python_timeout_seconds": 900,
    }


@pytest.mark.parametrize("failure", [None, "empty", "launch", "incorrect"])
def test_cuda_driver_probe_checks_result_and_cleans_context(monkeypatch, failure):
    from sandbox import native_gpu_probe

    calls = []

    class Function:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            calls.append(self.name)
            if self.name == "cuDeviceGetCount":
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int))[0] = (
                    0 if failure == "empty" else 1
                )
            elif self.name == "cuDeviceGet":
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_int))[0] = 0
            elif self.name == "cuDeviceGetUuid":
                ctypes.memset(args[0], 0x11, 16)
            elif self.name in {"cuCtxCreate_v2", "cuModuleLoadData", "cuModuleGetFunction"}:
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_void_p))[0] = 123
            elif self.name == "cuMemAlloc_v2":
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_uint64))[0] = 456
            elif self.name == "cuLaunchKernel":
                assert ctypes.cast(args[9][0], ctypes.POINTER(ctypes.c_uint64))[0] == 456
                return 999 if failure == "launch" else 0
            elif self.name == "cuMemcpyDtoH_v2":
                assert "cuLaunchKernel" in calls and "cuCtxSynchronize" in calls
                ctypes.cast(args[0], ctypes.POINTER(ctypes.c_uint))[0] = (
                    0 if failure == "incorrect" else 42
                )
            return 0

    class Driver:
        def __getattr__(self, name):
            return Function(name)

    monkeypatch.setattr(native_gpu_probe.C, "CDLL", lambda name: Driver())
    if failure:
        with pytest.raises(RuntimeError):
            native_gpu_probe.probe()
    else:
        assert native_gpu_probe.probe() == {"cuda_kernel_verified": True, "devices": [UUID0]}
    if failure != "empty":
        assert calls[-3:] == ["cuModuleUnload", "cuMemFree_v2", "cuCtxDestroy_v2"]


@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("RUN_SANDBOX_LINUX_TESTS") != "1",
    reason="Requires Linux namespaces; no GPU needed for device mount regression",
)
def test_real_linux_explicit_device_bind_retains_isolation(tmp_path):
    backend = NativeBackend(tmp_path, profile="standard")
    try:
        backend.gpu = NativeGPU("nvidia", "all", (DeviceNode.read(Path("/dev/null")),))
        code = """
import errno, pathlib, socket
with open('/dev/null', 'wb') as device: device.write(b'test')
assert not pathlib.Path('/dev/dxg').exists()
assert not list(pathlib.Path('/dev').glob('nvidia*'))
try: socket.socket()
except OSError as error: assert error.errno == errno.EPERM
else: raise AssertionError('network unexpectedly allowed')
"""
        outcome = backend.execute(tmp_path, "run_python", {"code": code})
        assert outcome.success and outcome.data["exit_code"] == 0, outcome
    finally:
        backend.close()


REAL_GPU = pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("RUN_NATIVE_GPU_TESTS") != "1",
    reason="Requires Linux/WSL2 NVIDIA GPU, drivers, bubblewrap and usable namespaces",
)


@REAL_GPU
def test_real_native_gpu_kernel_and_isolation(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("must remain hidden")
    outside = tmp_path / "private.txt"
    outside.write_text("outside")
    selection = os.getenv("NATIVE_TEST_GPUS", "all")
    with_gpu = NativeBackend(root) if selection == "all" else NativeBackend(root, gpus=selection)
    try:
        assert with_gpu.gpu_probe["cuda_kernel_verified"]
        code = f"""
import pathlib, socket, subprocess, sys
for path in ('.env', {str(outside)!r}):
    try: pathlib.Path(path).read_text()
    except OSError: pass
    else: raise AssertionError('outside/protected read allowed')
for family in (socket.AF_INET, socket.AF_UNIX):
    try: socket.socket(family)
    except OSError: pass
    else: raise AssertionError('network allowed')
subprocess.run(
    [sys.executable, '-I', {str(with_gpu.runtime / "sandbox/native_gpu_probe.py")!r}], check=True
)
"""
        if with_gpu.wsl_drivers:
            expected = sorted(path.name for path in with_gpu.wsl_drivers.packages)
            assert expected
            for path in ("/usr/lib/wsl/drivers", "/lib/wsl/drivers"):
                code += (
                    f"\nassert sorted(p.name for p in pathlib.Path({path!r}).iterdir())"
                    f" == {expected!r}\n"
                )
        executed = with_gpu.execute(root, "run_python", {"code": code, "timeout_seconds": 120})
        assert executed.success and executed.data["exit_code"] == 0, executed
        report = with_gpu.execute(root, "get_execution_environment", {"sections": ["execution"]})
        assert report.data["execution"]["gpu_access"]["enabled"]
        assert report.data["execution"]["tool_limits"]["python_max_timeout_seconds"] == 900
    finally:
        with_gpu.close()
    without_gpu = NativeBackend(root, profile="standard")
    try:
        code = (
            "import pathlib; assert not list(pathlib.Path('/dev').glob('nvidia*')); "
            "assert not pathlib.Path('/dev/dxg').exists()"
        )
        if without_gpu.wsl_drivers:
            code += "; assert not list(pathlib.Path('/usr/lib/wsl/drivers').iterdir())"
        executed = without_gpu.execute(root, "run_python", {"code": code})
        assert executed.data["exit_code"] == 0, executed
    finally:
        without_gpu.close()


@REAL_GPU
@pytest.mark.skipif(
    os.getenv("RUN_NATIVE_GPU_OPERATORS") != "1",
    reason="Also requires installed PyTorch, Triton and CUDA Toolkit",
)
def test_real_native_torch_triton_cuda_operators(tmp_path):
    backend = NativeBackend(tmp_path, gpus=os.getenv("NATIVE_TEST_GPUS", "all"))
    try:
        outcome = backend.execute(
            tmp_path,
            "run_command",
            {
                "command": [
                    str(backend.python),
                    "-I",
                    "-c",
                    f"import sys, runpy; sys.path.insert(0, {str(backend.runtime)!r}); "
                    "runpy.run_module('sandbox.operator_smoke', run_name='__main__')",
                ],
                "timeout_seconds": 900,
            },
        )
        assert outcome.success and outcome.data["exit_code"] == 0, outcome
    finally:
        backend.close()
