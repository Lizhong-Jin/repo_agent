"""Optional trusted fd primitives; no fallback after explicit Rust selection."""

import os

from .cancellation import current_cancellation


class RustFilesystem:
    def __init__(self):
        try:
            import rust_backend
        except ImportError as error:
            raise RuntimeError("Rust 文件扫描扩展未安装；请构建并安装 rust/ 扩展") from error
        if getattr(rust_backend, "FILESYSTEM_API_VERSION", None) != 2:
            raise RuntimeError("Rust 文件扫描接口版本不兼容；请重新构建并安装扩展")
        self.native = rust_backend

    def directory(self, fd, parts):
        return self.native.open_directory_at(fd, [os.fsencode(part) for part in parts])

    def names(self, fd):
        return [
            os.fsdecode(name) for name in self.native.list_directory(fd, current_cancellation())
        ]

    def stat_many(self, fd, names):
        results = self.native.stat_many(
            fd, [os.fsencode(name) for name in names], current_cancellation()
        )
        # CPython 3.11 macOS lacks the birthtime_ns slot added in 3.12.
        if not hasattr(os.stat_result, "st_birthtime_ns"):
            for item in results:
                if not isinstance(item, int):
                    item[1].pop("st_birthtime_ns", None)
        return [
            OSError(item, os.strerror(item), name)
            if isinstance(item, int)
            else os.stat_result(item[0], item[1])
            for name, item in zip(names, results, strict=True)
        ]

    def scan_metadata(self, fd, names):
        method = getattr(self.native, "scan_metadata", None)
        if method is None:
            # API 2 wheels predating the compact capability remain compatible.
            return self.stat_many(fd, names)
        packed = b"\0".join(os.fsencode(name) for name in names) + b"\0" if names else b""
        results = method(fd, packed, current_cancellation())
        return [
            OSError(item, os.strerror(item), name) if isinstance(item, int) else item
            for name, item in zip(names, results, strict=True)
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
