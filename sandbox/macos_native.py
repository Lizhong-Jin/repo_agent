"""macOS Seatbelt policy and enforcement; common lifecycle lives separately."""

import json
import platform
import sys
from pathlib import Path
from time import perf_counter

from host_support.languages import sandbox_environment
from tools._internal.file_policy import PROTECTED_NAMES, PROTECTED_SUFFIXES

from .native_common import NativeBackendBase
from .posix_execution import PosixNativeExecutionAdapter


def _quoted(value):
    text = str(value)
    if any(ord(char) < 32 for char in text):
        raise ValueError("Native 沙箱路径不能包含控制字符")
    return json.dumps(text, ensure_ascii=False)


def _pattern(text):
    # Seatbelt uses POSIX regexes, not Python's (?i) or non-capturing groups.
    return "".join(
        f"[{char.lower()}{char.upper()}]"
        if char.isalpha()
        else f"[{char}]"
        if char in ".-"
        else char
        for char in text
    )


METAL_COMPUTE_RULES = (
    '(allow iokit-open (iokit-user-client-class "AGXDeviceUserClient"))',
    '(allow mach-lookup (xpc-service-name "com.apple.MTLCompilerService"))',
)


def seatbelt_profile(
    workspace, scratch, read_paths, protected_paths, *, git_read=False, metal=False
):
    """Host-generated policy. Later deny rules also cover newly created names."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        # Never expose kern.procargs*: the host process environment contains keys.
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.") '
        '(sysctl-name "kern.ostype" "kern.osrelease" "kern.osversion" "kern.osproductversion" '
        '"kern.argmax" "kern.maxfiles" "kern.maxfilesperproc" "kern.maxproc" '
        '"kern.hostname" "kern.boottime" "kern.usrstack64" "kern.version"))',
        "(allow file-read-metadata)",
        # dyld/libignition opens / as an openat root during process startup.
        '(allow file-read* (literal "/"))',
        '(allow file-read* file-write-data (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))',
    ]
    if metal:
        # Apple Silicon compute only. No blanket IOKit/IPC, IOSurface, WindowServer,
        # host cache access or file-issue-extension grant. See docs/metal-validation.md.
        lines.extend(METAL_COMPUTE_RULES)
    for path in sorted(set(map(str, [workspace, scratch, *read_paths]))):
        lines.append(f"(allow file-read* file-map-executable (subpath {_quoted(path)}))")
    for path in (workspace, scratch):
        lines.append(f"(allow file-write* (subpath {_quoted(path)}))")
        # Do not rename/remove the allowed root itself.
        lines.append(f"(deny file-write-unlink (literal {_quoted(path)}))")
    # Protect the interpreter/dependencies even when installed inside the workspace.
    for path in read_paths:
        lines.append(f"(deny file-write* (subpath {_quoted(path)}))")
    names = "|".join(_pattern(name) for name in sorted(PROTECTED_NAMES - {".git"}))
    suffixes = "|".join(_pattern(suffix) for suffix in PROTECTED_SUFFIXES)
    pattern = f"(^|/)({names}|{_pattern('.env')}([.][^/]*)?|[^/]*({suffixes}))(/|$)"
    lines.append(f'(deny file-read* file-write* (regex #"{pattern}"))')
    git_ops = "file-write*" if git_read else "file-read* file-write*"
    lines.append(f'(deny {git_ops} (regex #"(^|/){_pattern(".git")}(/|$)"))')
    for path in protected_paths:
        lines.append(f"(deny file-read* file-write* (subpath {_quoted(path)}))")
    for path in (*protected_paths, *read_paths):
        # Ancestor renames must not relocate a protected subtree into an allowed path.
        for parent in Path(path).parents:
            if parent == workspace or not parent.is_relative_to(workspace):
                break
            lines.append(f"(deny file-write-unlink (literal {_quoted(parent)}))")
    # Deny hard-link creation even from otherwise readable toolchain paths.
    lines.extend(["(deny file-link)", "(deny network*)"])
    return "\n".join(lines) + "\n"


class MacOSNativeBackend(NativeBackendBase):
    execution_adapter_type = PosixNativeExecutionAdapter
    platform_name = "macos"
    isolation = "seatbelt"
    temporary_root = "/private/tmp"
    _metal_compute = False
    gpu_probe = None

    def _platform_setup(self):
        if sys.platform != "darwin":
            raise ValueError("native 沙箱仅支持 macOS/Linux；不会退回未隔离执行")
        from host_support.rust_filesystem import select_directory_backend

        self.directory_backend = select_directory_backend()
        self.executable = Path("/usr/bin/sandbox-exec")
        if not self.executable.is_file():
            raise ValueError("未找到 macOS sandbox-exec；不会退回未隔离执行")
        supported = platform.machine() == "arm64"
        if self.requested_profile == "metal" and not supported:
            raise ValueError("Metal native 当前仅支持 Apple Silicon；需使用 arm64 Python")
        # Apple Silicon always includes an Apple GPU. Treat the architecture as
        # a candidate, like Linux's device nodes, then require real sandbox compute.
        self._metal_compute = supported and self.requested_profile in {"auto", "metal"}
        self.gpu_probe = None
        if self._metal_compute:
            self.command_timeout_seconds = self.python_timeout_seconds = 900

    def execution_context(self):
        result = super().execution_context()
        result["gpu_access"] = {
            "enabled": self._metal_compute and self.gpu_probe is not None,
            "requested_profile": self.requested_profile,
            "profile": "metal" if self._metal_compute else "standard",
            "kind": "metal" if self._metal_compute else None,
            "selection": "default" if self._metal_compute else None,
            "device": self.gpu_probe["device"] if self.gpu_probe else None,
            "startup_probe": self.gpu_probe,
            "memory_limit": None,
            "exclusive": False,
            "disabled_reason": (
                "已显式关闭 GPU"
                if self.requested_profile == "standard"
                else "Metal native 当前仅支持 Apple Silicon / arm64 Python"
            )
            if not self._metal_compute
            else None,
        }
        return result

    def _check_workspace(self):
        backend = getattr(self, "directory_backend", None)
        if backend is None:
            return super()._check_workspace()
        backend.check_workspace(self.workspace)

    def _read_paths(self):
        paths = {
            self.runtime,
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            # Never allow /System wholesale: /System/Volumes/Data aliases user data.
            Path("/System/Library"),
            Path("/System/Cryptexes"),
            Path("/System/Volumes/Preboot/Cryptexes"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library/Apple"),
            Path("/Library/Developer"),
            Path("/Library/Frameworks"),
            Path("/opt/homebrew"),
            Path("/private/etc"),
            Path("/private/var/db/dyld"),
            Path("/private/var/db/timezone"),
        }
        # Include active dependency directories, but never sys.path's project entry.
        paths.update(
            Path(path).resolve()
            for path in sys.path
            if path and Path(path).name in {"site-packages", "dist-packages"}
        )
        return tuple(sorted(paths, key=str))

    def _environment(self, scratch):
        environment = sandbox_environment(self.python, scratch, platform="darwin")
        # A CPU fallback must not masquerade as a successful MPS computation.
        environment["PYTORCH_ENABLE_MPS_FALLBACK"] = "0"
        return environment

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        profile = control / "policy.sb"
        profile.write_text(
            seatbelt_profile(
                self.workspace,
                scratch,
                read_paths,
                self.protected_paths,
                git_read=git_read,
                metal=self._metal_compute,
            )
        )
        return [str(self.executable), "-f", str(profile), *command]

    def _preflight(self):
        started = perf_counter()
        # Verify actual kernel enforcement, rather than trusting executable presence.
        denied = self.directory / "denied.txt"
        denied.write_text("native sandbox probe")
        code = (
            "import errno, os, pathlib, socket, subprocess, sys, tempfile\n"
            "def blocked(action):\n"
            " try: action()\n"
            " except OSError as e:\n"
            "  assert e.errno in (errno.EPERM, errno.EACCES), repr(e)\n"
            " else: raise AssertionError('sandbox restriction missing')\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).read_text())\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).write_text('changed'))\n"
            "blocked(lambda: socket.socket().connect(('127.0.0.1', 9)))\n"
            "p = pathlib.Path(tempfile.gettempdir()) / 'probe'\n"
            "p.write_text('ok'); assert p.read_text() == 'ok'; p.unlink()\n"
            "blocked(lambda: (p.parent / '.ENV').write_text('blocked'))\n"
            f"with tempfile.NamedTemporaryFile(dir={str(self.workspace)!r}, "
            "prefix='.native-probe-') as f:\n"
            " f.write(b'probe'); f.flush()\n"
            " blocked(lambda: os.link(f.name, str(p)))\n"
            "subprocess.run([sys.executable, '-I', '-c', "
            f'"import pathlib; pathlib.Path({str(denied)!r}).read_text()"], '
            "check=False, capture_output=True).returncode != 0 or sys.exit(1)\n"
            "print('native-ok')\n"
        )
        result = self._run([str(self.python), "-I", "-c", code], timeout=15)
        if result.exit_code != 0 or result.stdout.strip() != "native-ok" or not self.healthy:
            raise ValueError(
                "macOS 原生沙箱自检失败；不会退回未隔离执行。"
                "当前系统或外层沙箱可能不允许 sandbox-exec。"
                + (f"\n{result.stderr[:1500]}" if result.stderr else "")
            )
        self.preflight_metrics = {"isolation_ms": (perf_counter() - started) * 1000}
        if self._metal_compute:
            started = perf_counter()
            result = self._run(
                [str(self.python), "-I", str(self.runtime / "sandbox/metal_probe.py")],
                timeout=45,
                max_output_bytes=16 * 1024,
            )
            self._validate_metal_preflight(result)
            self.preflight_metrics["gpu_ms"] = (perf_counter() - started) * 1000

    def _validate_metal_preflight(self, result):
        from .metal_probe import valid_report

        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError):
            report = None
        if (
            result.exit_code != 0
            or result.timed_out
            or result.stdout_truncated
            or not self.healthy
            or not valid_report(report)
        ):
            raise ValueError(
                "macOS native Metal 自检失败；未退回 CPU 或放宽隔离。"
                "请检查系统 GPU / 外层沙箱限制；可用 --sandbox-profile standard 关闭 GPU。\n"
                + (result.stderr + result.stdout)[-2000:]
            )
        self.gpu_probe = report
