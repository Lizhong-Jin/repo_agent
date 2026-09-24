"""Linux native backend: bubblewrap namespaces, read-only mounts and seccomp.

No daemon, image, root or sudo is used. The mounted workspace is the original.
Existing protected paths are masked on every invocation. Unlike Seatbelt, Linux
mount rules do not filter future filenames; see docs/native-sandbox.md.
"""

import errno
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from tools._internal.file_policy import is_protected_name

from .linux_gpu import NativeGPU
from .native import NativeBackend


def _outermost(paths):
    """Keep path spelling (including /bin aliases) when pruning nested mounts."""
    result = []
    for path in sorted(set(paths), key=lambda p: (len(p.parts), str(p))):
        if not any(path.is_relative_to(parent) for parent in result):
            result.append(path)
    return result


class LinuxNativeBackend(NativeBackend):
    platform_name = "linux"
    isolation = "bubblewrap+seccomp"
    temporary_root = "/tmp"
    gpu = None
    gpu_probe = None

    def _platform_setup(self):
        if sys.platform != "linux":
            raise ValueError("Linux native 后端只能在 Linux 上运行")
        # Do not search the workspace or a model-controlled PATH for the launcher.
        executable = shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin")
        if not executable:
            raise ValueError(
                "Linux native 缺少 bubblewrap (bwrap)；请安装 bubblewrap 和 libseccomp2 "
                "（Fedora 使用 libseccomp）。不会退回未隔离执行。"
            )
        self.executable = Path(executable)
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
            self.runtime, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
            # Do not mount /etc or /home wholesale. Runtime loader data is enough.
            Path("/usr"), Path("/bin"), Path("/sbin"), Path("/lib"), Path("/lib64"),
            Path("/etc/ld.so.cache"), Path("/etc/ld.so.conf"), Path("/etc/ld.so.conf.d"),
            Path("/etc/localtime"), Path("/etc/timezone"),
        }
        paths.update(
            Path(path).resolve() for path in sys.path
            if path and Path(path).name in {"site-packages", "dist-packages"}
        )
        if self.gpu:
            paths.update(self.gpu.read_paths)
        return tuple(sorted((p for p in paths if p.exists()), key=str))

    def _check_workspace(self):
        super()._check_workspace()
        for path in self.read_paths:
            if self.workspace.is_relative_to(path):
                raise ValueError("Linux native 工作区不能位于只读系统或解释器目录内")
        for path in (Path("/proc"), Path("/dev"), Path("/sys")):
            if self.workspace.is_relative_to(path) or path.is_relative_to(self.workspace):
                raise ValueError("Linux native 工作区与系统虚拟文件系统冲突")
        for directory, _, names in os.walk(self.workspace, followlinks=False):
            for name in names:
                mode = (Path(directory) / name).lstat().st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                    raise ValueError("Linux native 工作区含 socket/FIFO/设备等特殊文件，拒绝执行")

    def _mount_policy(self, read_paths, *, git_read):
        masks, git_paths = [], []
        roots = _outermost([self.workspace, *read_paths])

        def inaccessible(error):
            # Read-only system trees can contain root-only directories (e.g.
            # WSL's /lib/modules/.../lost+found). Lack of list permission does
            # NOT prevent opening known filenames in an execute-only directory.
            # Hide the entire unscanned subtree, never simply skip its contents.
            if (not isinstance(error, PermissionError)
                    or error.errno not in {errno.EACCES, errno.EPERM}
                    or not error.filename):
                raise error
            path = Path(error.filename)
            if (not path.is_absolute() or not path.is_relative_to(root)
                    or path.is_relative_to(self.workspace)
                    or path.resolve(strict=True).is_relative_to(self.workspace)):
                raise error
            masks.append(path)

        def protected(path):
            if any(path == p or path.is_relative_to(p) for p in self.protected_paths):
                return True
            if path.name.lower() == ".git" and git_read:
                git_paths.append(path)
                return False
            # Check the leaf: a readable .git must still hide its credential/log children.
            return is_protected_name(path.name)

        for root in roots:
            if protected(root):
                masks.append(root)
                continue
            if not root.is_dir():
                continue
            for directory, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
                for name in list(dirs) + names:
                    path = Path(directory) / name
                    if protected(path):
                        # Read-only toolchains often contain cert.pem symlinks. Hide
                        # their containing directory instead of following a mount
                        # target into another subtree. Workspace aliases fail closed.
                        masks.append(path.parent if path.is_symlink()
                                     and not path.is_relative_to(self.workspace) else path)
                        if name in dirs:
                            dirs.remove(name)
        masks = _outermost(masks)
        for path in (*masks, *git_paths):
            if path.is_symlink():
                raise ValueError(f"Linux native 受保护挂载点不能是符号链接：{path}")
        return masks, git_paths

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        masks, git_paths = self._mount_policy(read_paths, git_read=git_read)
        # Placeholders are never exposed by a writable mount, even with project code
        # running as the same UID. Empty files/dirs deny reads as well as writes.
        hidden_file, hidden_dir = control / "hidden-file", control / "hidden-dir"
        hidden_file.touch(mode=0)
        hidden_dir.mkdir(mode=0)
        argv = [
            str(self.executable), "--unshare-user", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts", "--disable-userns", "--assert-userns-disabled",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(self.workspace), str(self.workspace),
            "--bind", str(scratch), str(scratch),
        ]
        if self.gpu:
            argv.extend(self.gpu.mount_args())
        # A directory that is a mountpoint cannot be renamed. Guard ancestors of
        # fixed protected paths and embedded runtimes, otherwise a command could
        # move their parent and expose the original data on the next invocation.
        guards = set()
        for path in (*self.protected_paths, *read_paths):
            for parent in path.parents:
                if parent == self.workspace or not parent.is_relative_to(self.workspace):
                    break
                if parent.is_dir():
                    guards.add(parent)
        for path in sorted(guards, key=lambda p: (len(p.parts), str(p))):
            argv.extend(["--bind", str(path), str(path)])
        for path in _outermost(read_paths):
            argv.extend(["--ro-bind", str(path), str(path)])
        for path in git_paths:
            argv.extend(["--ro-bind", str(path), str(path)])
        for path in masks:
            source = hidden_dir if path.is_dir() else hidden_file
            argv.extend(["--ro-bind", str(source), str(path)])
        # The synthetic root and /tmp contain no host files. All persistent writes
        # are confined to the explicitly mounted workspace and per-call scratch.
        argv.extend([
            "--remount-ro", "/", "--remount-ro", "/tmp", "--",
            str(self.python), "-I", str(self.runtime / "sandbox/linux_exec.py"), *command,
        ])
        return argv

    def _environment(self, scratch):
        environment = super()._environment(scratch)
        environment["PATH"] = os.pathsep.join([
            str(self.python.parent), str(self.python.parent.parent / "lsp/node_modules/.bin"),
            "/usr/local/go/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ])
        environment["LANG"] = "C.UTF-8"
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
        return result

    def _preflight(self):
        if self.gpu and self.gpu.requested != "all":
            # Enumerate with a trusted system binary inside the sandbox, before
            # running any project code. Then reduce the device grant permanently.
            smi = shutil.which("nvidia-smi", path="/usr/bin:/bin:/usr/local/bin")
            if not smi:
                raise ValueError("选择单张 GPU 需要系统 nvidia-smi")
            inventory = self._run([
                smi, "--query-gpu=index,minor_number,uuid", "--format=csv,noheader,nounits",
            ], timeout=30)
            if inventory.exit_code != 0 or inventory.timed_out or not self.healthy:
                raise ValueError("沙箱内 NVIDIA GPU 枚举失败：" + inventory.stderr[:2000])
            self.gpu = self.gpu.select(inventory.stdout)
        denied = self.directory / "denied.txt"
        denied.write_text("private host data")
        # Exercise namespaces AND the seccomp launcher. Missing files in the private
        # mount namespace are expected ENOENT rather than Seatbelt's EPERM.
        code = f'''
import ctypes, errno, os, pathlib, socket, subprocess, sys, tempfile
def blocked(action):
    try:
        action()
    except OSError as e:
        assert e.errno in (errno.EPERM, errno.EACCES, errno.ENOENT, errno.EROFS), repr(e)
    else:
        raise AssertionError('native restriction missing')
blocked(lambda: pathlib.Path({str(denied)!r}).read_text())
blocked(lambda: pathlib.Path({str(denied)!r}).write_text('changed'))
blocked(lambda: socket.socket())
blocked(lambda: socket.socket(socket.AF_UNIX))
blocked(lambda: pathlib.Path('/proc/1/root' + {str(denied)!r}).read_text())
libc = ctypes.CDLL(None, use_errno=True)
assert libc.unshare(0x10000000) == -1 and ctypes.get_errno() == errno.EPERM
p = pathlib.Path(tempfile.gettempdir()) / 'probe'
p.write_text('ok'); assert p.read_text() == 'ok'; p.unlink()
with tempfile.NamedTemporaryFile(dir={str(self.workspace)!r}, prefix='.native-probe-') as f:
    f.write(b'probe'); f.flush()
    blocked(lambda: os.link(f.name, str(p)))
child = subprocess.run([sys.executable, '-I', '-c', 'import socket; socket.socket()'], capture_output=True)
assert child.returncode != 0
print('native-ok')
'''
        result = self._run([str(self.python), "-I", "-c", code], timeout=30)
        if result.exit_code != 0 or result.stdout.strip() != "native-ok" or not self.healthy:
            raise ValueError(
                "Linux 原生沙箱自检失败；需要 bubblewrap、libseccomp 以及可用的非特权 user namespace。"
                "系统 AppArmor、sysctl 或外层容器可能限制 namespace；不会退回未隔离执行。\n"
                + result.stderr[:2000]
            )
        if self.gpu:
            self._gpu_preflight()

    def _gpu_preflight(self):
        result = self._run([
            str(self.python), "-I", str(self.runtime / "sandbox/native_gpu_probe.py"),
        ], timeout=90)
        try:
            report = json.loads(result.stdout)
            valid = (report["cuda_kernel_verified"] is True
                     and isinstance(report["devices"], list) and bool(report["devices"]))
            if self.gpu.visible_uuid:
                valid = valid and report["devices"] == [self.gpu.visible_uuid]
        except (ValueError, KeyError, TypeError):
            valid = False
        if (result.exit_code != 0 or result.timed_out or result.stdout_truncated
                or not self.healthy or not valid):
            raise ValueError(
                "Linux native CUDA 自检失败；未退回 CPU 或放宽隔离。"
                "请检查驱动、UVM 设备权限、libcuda/PTX JIT 库和所选 GPU；MIG/NVSwitch 暂不支持。\n"
                "如需关闭 GPU，可显式使用 --sandbox-profile standard。\n"
                + result.stderr[:2000]
            )
        self.gpu_probe = report
