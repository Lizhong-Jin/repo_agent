"""One image build entry point, shared auto-detection with task startup."""

import argparse
import subprocess
from pathlib import Path

from installer.installation import record_image
from installer.paths import docker_build_context, installation_root

from .environment import (
    CUDA_BASE,
    DEFAULT_IMAGE,
    STANDARD_BASE,
    _docker,
    detect_environment,
)


def build_command(root: Path, profile: str, image: str) -> list[str]:
    if profile not in {"standard", "cuda"}:
        raise ValueError("Build requires a resolved profile")
    return [
        _docker(),
        "build",
        "-f",
        str(root / "sandbox" / "Dockerfile"),
        "-t",
        image,
        "--build-arg",
        f"BASE_IMAGE={CUDA_BASE if profile == 'cuda' else STANDARD_BASE}",
        "--build-arg",
        f"SANDBOX_PROFILE={profile}",
        str(root),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="自动检测 Docker 主机并构建统一沙箱镜像")
    parser.add_argument(
        "--profile",
        choices=["auto", "standard", "cuda"],
        default="auto",
        help="通常无需指定；手动覆盖用于离线构建或诊断",
    )
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args()
    root = installation_root()
    try:
        environment = detect_environment(profile=args.profile, image=args.image)
        print(f"[构建环境：{environment.profile}] {environment.reason}", flush=True)
        with docker_build_context() as context:
            result = subprocess.run(
                build_command(context, environment.profile, args.image), check=False
            )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    if result.returncode == 0:
        record_image(root, args.image)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
