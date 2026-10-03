"""Linux native backend: bubblewrap namespaces, read-only mounts and seccomp.

No daemon, image, root or sudo is used. The mounted workspace is the original.
Existing protected paths are masked on every invocation. Unlike Seatbelt, Linux
mount rules do not filter future filenames; see docs/native-sandbox.md.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from time import perf_counter

from host_support.languages import sandbox_environment
from tools._internal.file_policy import PROTECTED_NAME_RULES

from .linux_gpu import NativeGPU
from .linux_mounts import MountTable
from .linux_policy import validate_workspace_file
from .native_common import NativeBackendBase
from .policy_scan import PolicyPlan, PolicyScanner, ScanFailure, ScanRequest
from .policy_scan import outermost as _outermost
from .policy_scanners import create_policy_scanner
from .wsl_drivers import WSLDriverStore


class LinuxNativeBackend(NativeBackendBase):
    platform_name = "linux"
    isolation = "bubblewrap+seccomp"
    temporary_root = "/tmp"
    gpu = None
    gpu_probe = None
    wsl_drivers = None

    def _platform_setup(self):
        if sys.platform != "linux":
            raise ValueError("Linux native 后端只能在 Linux 上运行")
        from host_support.rust_filesystem import select_directory_backend

        self.directory_backend = select_directory_backend()
        # Do not search the workspace or a model-controlled PATH for the launcher.
        executable = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
        if not executable:
            raise ValueError(
                "Linux native 缺少 bubblewrap (bwrap)；请安装 bubblewrap 和 libseccomp2 "
                "（Fedora 使用 libseccomp）。不会退回未隔离执行。"
            )
        self.executable = Path(executable)
        self.wsl_drivers = WSLDriverStore.detect()
        if self.requested_gpus is not None or self.requested_profile == "cuda":
            self.gpu = NativeGPU.discover(self.requested_gpus or "all")
        elif self.requested_profile == "auto":
            self.gpu = NativeGPU.detect()
        else:
            self.gpu = None
        if self.gpu:
            self.command_timeout_seconds = self.python_timeout_seconds = 900

    def _read_paths(self):
        paths = {
            self.runtime,
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            # Do not mount /etc or /home wholesale. Runtime loader data is enough.
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/etc/ld.so.cache"),
            Path("/etc/ld.so.conf"),
            Path("/etc/ld.so.conf.d"),
            Path("/etc/localtime"),
            Path("/etc/timezone"),
        }
        paths.update(
            Path(path).resolve()
            for path in sys.path
            if path and Path(path).name in {"site-packages", "dist-packages"}
        )
        if self.gpu:
            paths.update(self.gpu.read_paths)
        return tuple(sorted((p for p in paths if p.exists()), key=str))

    def _check_workspace(self):
        self._check_workspace_layout()
        super()._check_workspace()

    def _prepare_workspace(self):
        # File metadata checks are performed in _mount_policy, even for direct
        # _run calls and startup probes. Never carry observations to another run.
        self._check_workspace_layout()
        self._set_metric("last_workspace_check_ms", 0.0)

    def _check_workspace_layout(self):
        for path in self.read_paths:
            if self.workspace.is_relative_to(path):
                raise ValueError("Linux native 工作区不能位于只读系统或解释器目录内")
        for path in (Path("/proc"), Path("/dev"), Path("/sys")):
            if self.workspace.is_relative_to(path) or path.is_relative_to(self.workspace):
                raise ValueError("Linux native 工作区与系统虚拟文件系统冲突")

    def _check_workspace_file(self, path: str, info: os.stat_result):
        validate_workspace_file(path, info)

    def _policy_plan(self):
        # Only configuration is reusable across calls. Resolve aliases and inspect
        # existence/permissions again on every scan, including startup probes.
        key = (self.workspace, tuple(self.read_paths), tuple(self.protected_paths))
        plan = getattr(self, "_compiled_policy_plan", None)
        if (
            plan is None
            or (plan.workspace, plan.read_paths, plan.protected_paths) != key
            or plan.name_rules != PROTECTED_NAME_RULES
        ):
            plan = self._compiled_policy_plan = PolicyPlan.compile(
                *key, name_rules=PROTECTED_NAME_RULES
            )
        return plan

    def _mount_policy(self, read_paths, *, git_read):
        self._check_workspace_layout()
        table = MountTable.read()
        views, roots = (), ()
        if self.wsl_drivers:
            self.wsl_drivers.verify(table)
            views = self.wsl_drivers.views(read_paths)
            roots = tuple(
                view / package.name for view in views for package in self.wsl_drivers.packages
            )
        request = ScanRequest(
            read_paths=tuple(read_paths),
            git_read=git_read,
            mount_snapshot=table.text,
            pruned_paths=tuple(views),
            extra_roots=tuple(roots),
        )
        # Lazy composition also supports scanner-only benchmarks without launching
        # a backend. The selected engine itself keeps no filesystem observations.
        scanner = getattr(self, "_policy_scanner", None)
        if scanner is None:
            scanner = create_policy_scanner()
            self._policy_scanner: PolicyScanner = scanner
        try:
            result = scanner.scan(self._policy_plan(), request)
        except ScanFailure as failure:
            self._set_metric("last_policy_metrics", failure.metrics)
            raise failure.error from None
        self._set_metric("last_policy_metrics", dict(result.metrics))
        if self.wsl_drivers:
            self.wsl_drivers.verify(table)
        return list(result.masks), list(result.git_paths)

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        masks, git_paths = self._mount_policy(read_paths, git_read=git_read)
        materialization_started = perf_counter()
        plan = self._policy_plan()
        # Placeholders are never exposed by a writable mount, even with project code
        # running as the same UID. Empty files/dirs deny reads as well as writes.
        hidden_file, hidden_dir = control / "hidden-file", control / "hidden-dir"
        hidden_file.touch(mode=0)
        hidden_dir.mkdir(mode=0)
        argv = [
            str(self.executable),
            "--unshare-user",
            "--unshare-pid",
            "--unshare-net",
            "--unshare-ipc",
            "--unshare-uts",
            "--disable-userns",
            "--assert-userns-disabled",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(self.workspace),
            str(self.workspace),
            "--bind",
            str(scratch),
            str(scratch),
        ]
        if self.gpu:
            argv.extend(self.gpu.mount_args())
        # A directory that is a mountpoint cannot be renamed. Guard ancestors of
        # fixed protected paths and embedded runtimes, otherwise a command could
        # move their parent and expose the original data on the next invocation.
        guards = set(plan.guard_candidates)
        # Per-call request files normally live outside the workspace. Keep this
        # path general without retaining their locations in the static plan.
        extra_paths = tuple(path for path in read_paths if path not in plan.read_paths)
        for path in extra_paths:
            for parent in path.parents:
                if parent == self.workspace or not parent.is_relative_to(self.workspace):
                    break
                guards.add(parent)
        for path in sorted(guards, key=lambda p: (len(p.parts), str(p))):
            if path.is_dir():
                argv.extend(["--bind", str(path), str(path)])
        mounts = (
            plan.mount_roots if tuple(read_paths) == plan.read_paths else _outermost(read_paths)
        )
        for path in mounts:
            argv.extend(["--ro-bind", str(path), str(path)])
        if self.wsl_drivers:
            argv.extend(self.wsl_drivers.mount_args(self.wsl_drivers.views(read_paths)))
        for path in git_paths:
            argv.extend(["--ro-bind", str(path), str(path)])
        for path in masks:
            source = hidden_dir if path.is_dir() else hidden_file
            argv.extend(["--ro-bind", str(source), str(path)])
        # The synthetic root and /tmp contain no host files. All persistent writes
        # are confined to the explicitly mounted workspace and per-call scratch.
        argv.extend(
            [
                "--remount-ro",
                "/",
                "--remount-ro",
                "/tmp",
                "--",
                str(self.python),
                "-I",
                str(self.runtime / "sandbox/linux_exec.py"),
                *command,
            ]
        )
        self._get_metric("last_policy_metrics")["materialization_ms"] = (
            perf_counter() - materialization_started
        ) * 1000
        return argv

    def _environment(self, scratch):
        environment = sandbox_environment(self.python, scratch, platform="linux")
        if self.gpu:
            environment.update(self.gpu.environment(scratch))
            bins = []
            if self.gpu.cuda_home:
                bins.append(str(self.gpu.cuda_home / "bin"))
            if self.gpu.kind == "wsl2":
                bins.append("/usr/lib/wsl/lib")
            environment["PATH"] = os.pathsep.join([*bins, environment["PATH"]])
        return environment

    def execution_context(self):
        result = super().execution_context()
        result["protected_paths_policy"] = "mask_existing_paths_at_call_start"
        result["writable_paths"].append("private device filesystem (/dev, /dev/shm)")
        result["persistence"]["process_cleanup"] = "pid_namespace_and_host_supervision"
        result["gpu_access"] = {
            "enabled": self.gpu is not None,
            "requested_profile": self.requested_profile,
            "profile": "cuda" if self.gpu else "standard",
            "kind": self.gpu.kind if self.gpu else None,
            "selection": self.gpu.requested if self.gpu else None,
            "device_paths": [str(node.path) for node in self.gpu.devices] if self.gpu else [],
            "startup_probe": self.gpu_probe,
            "memory_limit": None,
            "exclusive": False,
        }
        if self.wsl_drivers:
            result["gpu_access"]["driver_store_packages"] = [
                str(path) for path in self.wsl_drivers.packages
            ]
        return result

    def _preflight(self):
        driver_discovery_ms = None
        if self.wsl_drivers and self.gpu and self.gpu.kind == "wsl2":
            started = perf_counter()
            discovery = self._run(
                [
                    str(self.python),
                    "-I",
                    str(self.runtime / "sandbox/wsl_gpu_probe.py"),
                ],
                timeout=30,
            )
            if (
                discovery.exit_code != 0
                or discovery.timed_out
                or discovery.stdout_truncated
                or not self.healthy
            ):
                raise ValueError(
                    "WSL 驱动包查询失败；不会暴露整个驱动存储。\n" + discovery.stderr[:2000]
                )
            self.wsl_drivers.select(discovery.stdout)
            driver_discovery_ms = (perf_counter() - started) * 1000
        if self.gpu and self.gpu.requested != "all":
            # Enumerate with a trusted system binary inside the sandbox, before
            # running any project code. Then reduce the device grant permanently.
            smi = shutil.which("nvidia-smi", path="/usr/bin:/bin:/usr/local/bin")
            if not smi:
                raise ValueError("选择单张 GPU 需要系统 nvidia-smi")
            inventory = self._run(
                [
                    smi,
                    "--query-gpu=index,minor_number,uuid",
                    "--format=csv,noheader,nounits",
                ],
                timeout=30,
            )
            if inventory.exit_code != 0 or inventory.timed_out or not self.healthy:
                raise ValueError("沙箱内 NVIDIA GPU 枚举失败：" + inventory.stderr[:2000])
            self.gpu = self.gpu.select(inventory.stdout)
        denied = self.directory / "denied.txt"
        denied.write_text("private host data")
        command = [
            str(self.python),
            "-I",
            str(self.runtime / "sandbox/linux_preflight.py"),
            "--workspace",
            str(self.workspace),
            "--denied",
            str(denied),
        ]
        if self.gpu:
            command.append("--gpu")
        # One policy/namespace, separate 30s isolation and 90s CUDA child deadlines.
        result = self._run(command, timeout=125 if self.gpu else 35)
        try:
            report = json.loads(result.stdout)
            isolation = report["isolation"]
            valid = (
                isolation["exit_code"] == 0
                and isolation["timed_out"] is False
                and isolation["stdout"].strip() == "native-ok"
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            report, isolation, valid = {}, {}, False
        if (
            not valid
            or result.timed_out
            or result.stdout_truncated
            or not self.healthy
            or (not self.gpu and result.exit_code != 0)
        ):
            raise ValueError(
                "Linux 原生沙箱自检失败；需要 bubblewrap、libseccomp 以及可用的非特权 "
                "user namespace。系统 AppArmor、sysctl 或外层容器可能限制 namespace；"
                "不会退回未隔离执行。\n"
                + str(isolation.get("stderr", ""))[:2000]
                + result.stderr[:2000]
            )
        self.preflight_metrics = {"isolation_ms": isolation.get("duration_ms")}
        if driver_discovery_ms is not None:
            self.preflight_metrics["driver_discovery_ms"] = driver_discovery_ms
        if self.gpu:
            self._validate_gpu_preflight(report.get("gpu"), result)
            self.preflight_metrics["gpu_ms"] = report["gpu"].get("duration_ms")

    def _validate_gpu_preflight(self, check, result):
        try:
            report = json.loads(check["stdout"])
            valid = (
                check["exit_code"] == 0
                and check["timed_out"] is False
                and report["cuda_kernel_verified"] is True
                and isinstance(report["devices"], list)
                and bool(report["devices"])
            )
            if self.gpu.visible_uuid:
                valid = valid and report["devices"] == [self.gpu.visible_uuid]
        except (ValueError, KeyError, TypeError):
            valid = False
        if (
            result.exit_code != 0
            or result.timed_out
            or result.stdout_truncated
            or not self.healthy
            or not valid
        ):
            diagnostic = check.get("stderr", "") if isinstance(check, dict) else ""
            raise ValueError(
                "Linux native CUDA 自检失败；未退回 CPU 或放宽隔离。"
                "请检查驱动、UVM 设备权限、libcuda/PTX JIT 库和所选 GPU；MIG/NVSwitch 暂不支持。\n"
                "如需关闭 GPU，可显式使用 --sandbox-profile standard。\n"
                + str(diagnostic)[:2000]
                + result.stderr[:2000]
            )
        self.gpu_probe = report
