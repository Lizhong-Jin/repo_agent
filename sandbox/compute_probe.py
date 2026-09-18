"""Report actual compute capabilities without treating a CPU fallback as GPU success."""

import argparse
import importlib
import json
import platform
import shutil
import subprocess


def collect() -> dict:
    report = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "torch": None,
        "triton": None,
        "cuda_available": False,
        "devices": [],
        "nvcc": None,
        "errors": {},
    }
    for name in ("torch", "triton"):
        try:
            module = importlib.import_module(name)
            report[name] = str(module.__version__)
            if name == "torch":
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
            report["errors"][name] = type(error).__name__
            if name == "torch":
                report["cuda_available"] = False
    nvcc = shutil.which("nvcc")
    if nvcc:
        try:
            result = subprocess.run([nvcc, "--version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                report["nvcc"] = result.stdout.strip()[-4096:]
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
        "--require-gpu",
        action="store_true",
        help="Fail unless CUDA GPU, PyTorch, Triton and nvcc are available",
    )
    args = parser.parse_args()
    report = collect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.require_gpu and not report["operator_environment_ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
