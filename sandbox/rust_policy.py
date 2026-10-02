"""Optional native implementation of the batch policy-scanning contract."""

import os
import sys
from pathlib import Path
from time import perf_counter

from host_support.cancellation import current_cancellation

from .linux_mounts import MountTable
from .policy_scan import PolicyPlan, ScanFailure, ScanRequest, ScanResult


class RustPolicyScanner:
    """One native call per scan, with fresh filesystem observations every time."""

    def __init__(self):
        if sys.platform not in {"linux", "darwin"}:
            raise ValueError("Rust policy scanner supports Linux and macOS testing only")
        try:
            import rust_backend
        except ImportError as error:
            raise RuntimeError(
                "Rust 扫描扩展未安装或无法加载；请先构建并安装 rust/ 下的扩展，"
                "或显式选择 AGENT_NATIVE_SCANNER=python"
            ) from error
        if rust_backend.API_VERSION != 1:
            raise RuntimeError("Rust 扫描扩展接口版本不兼容，请重新构建 rust/ 下的扩展")
        self._native = rust_backend
        self.last_diagnostics = None

    def scan(self, plan: PolicyPlan, request: ScanRequest) -> ScanResult:
        table = MountTable(request.mount_snapshot)
        config = {
            "workspace": os.fsencode(plan.workspace),
            "roots": [
                os.fsencode(p) for p in (*plan.roots(request.read_paths), *request.extra_roots)
            ],
            "protected_paths": [os.fsencode(p) for p in plan.protected_paths],
            "pruned_paths": [os.fsencode(p) for p in request.pruned_paths],
            "names": [name.encode("utf-8", "surrogatepass") for name in plan.name_rules.names],
            "prefixes": [
                name.encode("utf-8", "surrogatepass") for name in plan.name_rules.prefixes
            ],
            "suffixes": [
                name.encode("utf-8", "surrogatepass") for name in plan.name_rules.suffixes
            ],
            "git_read": request.git_read,
            "mount_snapshot": request.mount_snapshot.encode("utf-8"),
            "mounts": [(os.fsencode(m.path), m.filesystem) for m in table.mounts.values()],
            # pathlib >=3.14 returns False for all OSError from is_dir/is_symlink.
            "ignore_stat_errors": sys.version_info >= (3, 14),
            "cancellation": current_cancellation(),
        }
        self.last_diagnostics = None
        started = perf_counter()
        try:
            result = self._native.scan(config)
        except BaseException as error:
            # Binding failures/panics or a signal at GIL reattachment have no
            # normal native result. Still reject execution and preserve control flow.
            raise ScanFailure(
                error, {"complete": False, "scan_ms": (perf_counter() - started) * 1000}
            ) from error
        self.last_diagnostics = result.get("diagnostics")
        if result["error"] is not None:
            raise ScanFailure(result["error"], result["metrics"])
        return ScanResult(
            tuple(Path(os.fsdecode(p)) for p in result["masks"]),
            tuple(Path(os.fsdecode(p)) for p in result["git_paths"]),
            result["metrics"],
        )
