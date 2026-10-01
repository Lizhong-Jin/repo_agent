#!/usr/bin/env python3
"""Build the optional host Rust extension or its installable wheel."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from installer.rust_extension import build_rust_library, build_rust_wheel  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "wheel"):
        command = commands.add_parser(name, help="编译动态库" if name == "build" else "构建 wheel")
        command.add_argument("--output", type=Path, default=ROOT / "rust_wheels")
        command.add_argument("--offline", action="store_true", help="仅使用本地依赖")
        if name == "wheel":
            command.add_argument("--wheelhouse", type=Path, help="本地 maturin 构建依赖目录")
            command.add_argument("--compatibility", help="例如 manylinux_2_28；由 maturin 校验")
            command.add_argument(
                "--no-build-isolation",
                action="store_true",
                help="使用当前 Python 环境中已安装的 maturin",
            )
    args = parser.parse_args(argv)
    try:
        options = {"offline": args.offline}
        if args.command == "wheel":
            options.update(
                wheelhouse=args.wheelhouse.expanduser().resolve() if args.wheelhouse else None,
                compatibility=args.compatibility,
                build_isolation=not args.no_build_isolation,
            )
        build = build_rust_library if args.command == "build" else build_rust_wheel
        artifact = build(ROOT, sys.executable, args.output.expanduser().resolve(), **options)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Rust 构建失败：{error}\n")
    print(f"[OK] {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
