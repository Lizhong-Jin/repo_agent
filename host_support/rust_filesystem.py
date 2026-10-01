"""Optional trusted fd primitives; no fallback after explicit Rust selection."""

import os

from .cancellation import current_cancellation


class RustFilesystem:
    def __init__(self):
        try:
            import rust_backend
        except ImportError as error:
            raise RuntimeError("Rust 文件扫描扩展未安装；请构建并安装 rust/ 扩展") from error
        if getattr(rust_backend, "FILESYSTEM_API_VERSION", None) != 1:
            raise RuntimeError("Rust 文件扫描接口版本不兼容；请重新构建并安装扩展")
        self.native = rust_backend

    def directory(self, fd, parts):
        return self.native.open_directory_at(fd, [os.fsencode(part) for part in parts])

    def names(self, fd):
        return [
            os.fsdecode(name) for name in self.native.list_directory(fd, current_cancellation())
        ]

    def check_workspace(self, root):
        self.native.check_workspace(os.fsencode(root), current_cancellation())


def select_directory_backend():
    engine = os.environ.get("AGENT_NATIVE_SCANNER") or "python"
    if engine == "python":
        return None
    if engine == "rust":
        return RustFilesystem()
    raise ValueError("AGENT_NATIVE_SCANNER 必须为 python 或 rust")
