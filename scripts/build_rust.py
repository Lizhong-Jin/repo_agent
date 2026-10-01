#!/usr/bin/env python3
"""Build supported Rust targets, reporting missing toolchains per platform."""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from host_support.platforms import PlatformInfo  # noqa: E402
from installer.rust_extension import build_rust_library, build_rust_wheel  # noqa: E402
from installer.rust_targets import RUST_TRIPLES, prerequisites  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "wheel"):
        command = commands.add_parser(name, help="编译动态库" if name == "build" else "构建 wheel")
        command.add_argument("--output", type=Path, default=ROOT / "rust_wheels")
        command.add_argument("--offline", action="store_true", help="仅使用本地依赖")
        command.add_argument(
            "--target",
            action="append",
            choices=["all", "host", *RUST_TRIPLES],
            help="可重复指定；默认 all；host 仅编译本机",
        )
        command.add_argument("--check", action="store_true", help="仅检查工具链，不构建")
        command.add_argument("--wheelhouse", type=Path, help="本地 maturin 构建依赖目录")
        command.add_argument("--compatibility", help="Linux wheel 基线，默认 manylinux_2_28")
        command.add_argument(
            "--no-build-isolation",
            action="store_true",
            help="使用当前 Python 环境中已安装的 maturin",
        )
    args = parser.parse_args(argv)
    targets = []
    for selection in args.target or ["all"]:
        choices = (
            RUST_TRIPLES
            if selection == "all"
            else [PlatformInfo.detect().target if selection == "host" else selection]
        )
        targets.extend(target for target in choices if target not in targets)
    failures = []
    for target in targets:
        missing = prerequisites(target)
        needs_wheel = args.command == "wheel" or target != PlatformInfo.detect().target
        if needs_wheel and importlib.util.find_spec("pip") is None:
            missing.append("缺少 pip：python -m ensurepip --upgrade")
        if needs_wheel and args.no_build_isolation and importlib.util.find_spec("maturin") is None:
            missing.append("缺少 maturin：python -m pip install 'maturin>=1.9,<2'")
        if missing:
            failures.append(target)
            print(f"[MISSING] {target}: " + "；".join(missing), flush=True)
            continue
        if args.check:
            print(f"[READY] {target}", flush=True)
            continue
        try:
            build = build_rust_library if args.command == "build" else build_rust_wheel
            artifact = build(
                ROOT,
                sys.executable,
                args.output.expanduser().resolve(),
                target=target,
                offline=args.offline,
                wheelhouse=args.wheelhouse.expanduser().resolve() if args.wheelhouse else None,
                compatibility=args.compatibility,
                build_isolation=not args.no_build_isolation,
            )
            print(f"[OK] {target}: {artifact}", flush=True)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(target)
            print(f"[FAILED] {target}: {error}", flush=True)
    if failures:
        print("未完成的平台：" + ", ".join(failures) + "；已成功产物保留，可用 --target 单独重试。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
