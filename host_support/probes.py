"""Dependency availability probes, separate from actual sandbox enforcement tests."""

import shutil
from pathlib import Path

from .diagnostics import Diagnostic


def _native_rows(platform):
    if platform == "win32":
        return [
            ("OK", "原生沙箱", "Windows LPAC/Job；启动时验证实际隔离，Python/Git 在私有副本运行")
        ]
    if platform == "linux":
        import ctypes

        if not shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin"):
            return [
                (
                    "ERROR",
                    "原生沙箱",
                    "缺少 bubblewrap；Debian/Ubuntu 安装 bubblewrap libseccomp2，"
                    "Fedora 安装 bubblewrap libseccomp",
                )
            ]
        try:
            ctypes.CDLL("libseccomp.so.2")
        except OSError:
            return [("ERROR", "原生沙箱", "缺少 libseccomp.so.2；请安装 libseccomp2 或 libseccomp")]
        return [
            (
                "OK",
                "原生沙箱",
                "bubblewrap/libseccomp 存在；安装后将验证 user namespace、实际隔离和语言服务",
            )
        ]
    if platform != "darwin":
        return [
            (
                "ERROR",
                "原生沙箱",
                "native 仅支持 macOS/Linux/Windows x86_64",
            )
        ]
    if not Path("/usr/bin/sandbox-exec").is_file():
        return [
            (
                "ERROR",
                "原生沙箱",
                "缺少 /usr/bin/sandbox-exec；请选择 --mode docker 或 --mode local",
            )
        ]
    return [("OK", "原生沙箱", "sandbox-exec 存在；安装后将验证实际隔离和语言服务")]


def native_availability(platform):
    return [
        Diagnostic(level, component, detail) for level, component, detail in _native_rows(platform)
    ]
