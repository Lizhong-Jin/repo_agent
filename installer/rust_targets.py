"""Build target planning and actionable prerequisite checks (stdlib only)."""

import shutil
import subprocess
from pathlib import Path

from host_support.platforms import PlatformInfo

RUST_TRIPLES = {
    "linux-x86_64": "x86_64-unknown-linux-gnu",
    "linux-arm64": "aarch64-unknown-linux-gnu",
    "macos-x86_64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}


def prerequisites(target):
    """Report all missing prerequisites; never install or alter a toolchain."""
    if target not in RUST_TRIPLES:
        return [f"不支持的 Rust 平台：{target}"]
    missing = []
    if not shutil.which("cargo") or not shutil.which("rustc"):
        missing.append("请安装 Rust >= 1.85 和 rustup：https://rustup.rs")
    triple = RUST_TRIPLES[target]
    if shutil.which("rustc"):
        try:
            result = subprocess.run(
                ["rustc", "--print", "target-libdir", "--target", triple],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if not list(Path(result.stdout.strip()).glob("libstd-*")):
                missing.append(f"缺少目标标准库：rustup target add {triple}")
        except (OSError, subprocess.SubprocessError):
            missing.append(f"无法检查目标标准库：rustup target add {triple}")
    host = PlatformInfo.detect().target
    if target.startswith("macos-"):
        if not host.startswith("macos-"):
            missing.append("macOS 构建需要 Apple SDK；请在 macOS 主机或项目 macOS CI 上构建")
        else:
            try:
                subprocess.run(
                    ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                missing.append("缺少 Apple SDK/链接器：xcode-select --install")
    elif target != host and not shutil.which("zig"):
        missing.append(
            "缺少跨平台链接器 Zig：安装 Zig 并将 zig 加入 PATH（https://ziglang.org/download/）"
        )
    elif target == host and not shutil.which("cc"):
        missing.append("缺少 C 链接器：Debian/Ubuntu 执行 sudo apt install build-essential")
    return missing
