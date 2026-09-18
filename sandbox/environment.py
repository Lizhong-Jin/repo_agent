"""Detect compute support on the selected Docker daemon, not the CLI machine.

No workspace or credentials are mounted into the short-lived GPU probe. A
registered but broken NVIDIA runtime is an error, not a silent CPU fallback.
"""

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass

DEFAULT_IMAGE = "repo-agent-sandbox:v1"
PROFILE_LABEL = "org.repo-agent.profile"
CUDA_BASE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel"
STANDARD_BASE = "python:3.12-slim-bookworm"


@dataclass(frozen=True)
class DockerEnvironment:
    profile: str
    reason: str
    architecture: str


def _docker() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise ValueError("Docker 不可用；请安装并启动 Docker。")
    return executable


def _run(arguments: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [_docker(), *arguments], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Docker 检测失败或超时，请检查当前 Docker context。") from error


def detect_environment(*, profile: str = "auto", image: str = DEFAULT_IMAGE) -> DockerEnvironment:
    if profile not in {"auto", "standard", "cuda"}:
        raise ValueError("profile must be auto, standard or cuda")
    result = _run(["info", "--format", "{{json .}}"])
    if result.returncode:
        raise ValueError("无法连接 Docker；请检查服务、权限和当前 context。")
    try:
        info = json.loads(result.stdout)
        arch = info["Architecture"]
        if info["OSType"] != "linux":
            raise ValueError("沙箱需要 Linux 容器模式。")
        if not isinstance(arch, str) or not isinstance(info.get("Runtimes", {}), dict):
            raise TypeError("Invalid Docker info")
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Docker 返回了无效的主机信息。") from error
    if profile != "auto":
        if profile == "cuda" and arch not in {"x86_64", "amd64"}:
            raise ValueError("当前 CUDA 镜像仅支持 Linux x86_64。")
        return DockerEnvironment(profile, "使用显式指定的环境", arch)
    if "nvidia" not in info.get("Runtimes", {}):
        return DockerEnvironment("standard", "Docker 未配置 NVIDIA runtime，使用普通环境", arch)
    if arch not in {"x86_64", "amd64"}:
        raise ValueError("检测到 NVIDIA runtime，但当前 CUDA 镜像仅支持 Linux x86_64。")
    # Reuse the installed agent image offline. On first setup, use NVIDIA's
    # documented small Ubuntu probe rather than pulling the full CUDA image.
    available = _run(["image", "inspect", image, "--format", "{{.Id}}"])
    probe_image = image if available.returncode == 0 else "ubuntu:22.04"
    name = "repo-agent-gpu-probe-" + uuid.uuid4().hex
    try:
        result = _run(
            [
                "run",
                "--rm",
                "--name",
                name,
                "--pull=missing",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                "65534:65534",
                "--memory",
                "256m",
                "--pids-limit",
                "64",
                "--runtime=nvidia",
                "--gpus",
                "all",
                "--env",
                "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
                "--entrypoint",
                "nvidia-smi",
                probe_image,
                "-L",
            ],
            timeout=60,
        )
    finally:
        cleanup = _run(["rm", "-f", name], timeout=10)
        if cleanup.returncode and "No such container" not in cleanup.stderr:
            raise ValueError("GPU 检测容器清理失败，请检查 Docker。")
    if result.returncode == 0 and any(
        line.startswith("GPU ") for line in result.stdout.splitlines()
    ):
        return DockerEnvironment("cuda", "已在 Docker 容器中确认 NVIDIA GPU", arch)
    if "No devices were found" in result.stdout + result.stderr:
        return DockerEnvironment("standard", "NVIDIA runtime 可用，但未发现 GPU", arch)
    raise ValueError(
        "NVIDIA runtime 已配置，但 GPU 容器探测失败；请检查驱动和 NVIDIA Container Toolkit。"
        "如需普通环境，可显式使用 --sandbox-profile standard。"
    )


def check_image_profile(image: str, profile: str, *, allow_unlabelled: bool = False) -> None:
    result = _run(["image", "inspect", image, "--format", "{{json .Config.Labels}}"])
    if result.returncode:
        raise ValueError("沙箱镜像不存在；先运行 ./run_agent.sh --build-sandbox。")
    try:
        labels = json.loads(result.stdout) or {}
        actual = labels.get(PROFILE_LABEL)
    except (AttributeError, json.JSONDecodeError) as error:
        raise ValueError("Docker 返回了无效的镜像标签。") from error
    if actual is None and allow_unlabelled:
        return
    if actual != profile:
        raise ValueError(
            f"镜像环境 {actual or '未标记'} 与检测结果 {profile} 不一致；"
            "请运行 ./run_agent.sh --build-sandbox 重新构建。"
        )
