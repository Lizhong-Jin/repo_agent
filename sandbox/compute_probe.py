"""Report actual compute capabilities without treating a CPU fallback as GPU success."""

import argparse
import importlib
import json
import os
import platform
import shutil
import subprocess


def probe_mps(torch):
    report = {"status": "unavailable", "built": False, "available": False, "kernel_verified": False}
    try:
        backend = getattr(getattr(torch, "backends", None), "mps", None)
        if backend is None:
            report["reason"] = "pytorch_without_mps_backend"
            return report
        report["built"] = bool(backend.is_built())
        report["available"] = bool(backend.is_available())
        if not report["available"]:
            report["reason"] = "mps_not_available"
            return report
        matrix = torch.tensor([[1.0, 2.0], [3.0, 4.0]], device="mps", dtype=torch.float32)
        output = matrix @ matrix
        if matrix.device.type != "mps" or output.device.type != "mps":
            raise RuntimeError("MPS probe unexpectedly used another device")
        torch.mps.synchronize()
        if output.cpu().tolist() != [[7.0, 10.0], [15.0, 22.0]]:
            raise RuntimeError("MPS matrix multiplication returned incorrect results")
        report.update(status="available", kernel_verified=True)
    except Exception as error:
        report["error"] = str(error)[:1000]
    return report


def collect() -> dict:
    macos = platform.system() == "Darwin"
    if macos:
        # Must be set before importing torch in this isolated probe process.
        os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
    report = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "torch": None,
        "triton": None,
        "cuda_available": False,
        "devices": [],
        "nvcc": None,
        "torch_status": "unknown",
        "triton_status": "unknown",
        "nvcc_status": "missing",
        "errors": {},
    }
    if macos:
        report["mps"] = {"status": "unknown", "kernel_verified": False}
    for name in ("torch", "triton"):
        try:
            module = importlib.import_module(name)
            report[name] = str(module.__version__)
            report[name + "_status"] = "available"
            if name == "torch":
                if macos:
                    report["mps"] = probe_mps(module)
                report["torch_cuda_version"] = module.version.cuda
                report["cuda_available"] = bool(module.cuda.is_available())
                if report["cuda_available"]:
                    for index in range(module.cuda.device_count()):
                        props = module.cuda.get_device_properties(index)
                        report["devices"].append(
                            {
                                "index": index,
                                "name": props.name,
                                "compute_capability": [props.major, props.minor],
                                "total_memory_bytes": props.total_memory,
                            }
                        )
        except Exception as error:
            report[name + "_status"] = (
                "missing"
                if isinstance(error, ModuleNotFoundError) and error.name == name
                else "unavailable"
            )
            report["errors"][name] = type(error).__name__
            if name == "torch":
                report["cuda_available"] = False
                if macos:
                    report["mps"] = {
                        "status": report["torch_status"],
                        "kernel_verified": False,
                        "reason": "torch_import_failed",
                    }
    nvcc = shutil.which("nvcc")
    if nvcc:
        report["nvcc_status"] = "unavailable"
        try:
            result = subprocess.run([nvcc, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                report["nvcc"] = result.stdout.strip()[-4096:]
                report["nvcc_status"] = "available"
            else:
                report["errors"]["nvcc"] = f"exit {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as error:
            report["errors"]["nvcc"] = type(error).__name__
    report["operator_environment_ready"] = bool(
        report["cuda_available"]
        and report["devices"]
        and report["triton"]
        and report["nvcc"]
        and not report["errors"]
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-mps",
        action="store_true",
        help="Fail unless a real PyTorch MPS matrix multiplication succeeds",
    )
    parser.add_argument(
        "--require-gpu",
        action="store_true",
        help="Fail unless CUDA GPU, PyTorch, Triton and nvcc are available",
    )
    args = parser.parse_args()
    report = collect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_gpu and not report["operator_environment_ready"]:
        raise SystemExit(1)
    if args.require_mps and not report.get("mps", {}).get("kernel_verified"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
