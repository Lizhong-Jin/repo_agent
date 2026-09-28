"""Detect and grant NVIDIA devices for Linux native without privileged helpers."""

import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from .policy import SandboxPolicy


def _nvidia_cards():
    return sorted(p for p in Path("/dev").glob("nvidia*") if re.fullmatch(r"nvidia[0-9]+", p.name))


@dataclass(frozen=True)
class DeviceNode:
    path: Path
    identity: tuple[int, int, int]

    @classmethod
    def read(cls, path):
        try:
            info = path.lstat()
        except OSError as error:
            raise ValueError(
                f"GPU 设备不可用：{path}；请先在宿主机安装驱动并加载 NVIDIA UVM 模块"
            ) from error
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError(f"GPU 路径必须是字符设备，不能是普通文件或符号链接：{path}")
        return cls(path, (info.st_dev, info.st_ino, info.st_rdev))

    def verify(self):
        if self != self.read(self.path):
            raise ValueError(f"GPU 设备在会话中发生变化，请重启会话：{self.path}")


@dataclass(frozen=True)
class NativeGPU:
    kind: str
    requested: str
    devices: tuple[DeviceNode, ...]
    read_paths: tuple[Path, ...] = ()
    visible_uuid: str | None = None
    cuda_home: Path | None = None

    @classmethod
    def detect(cls):
        """No NVIDIA device means standard; broken detected devices still fail.

        WSL's dxg also exists for non-NVIDIA GPUs. Require the Windows-provided
        CUDA library before treating that shared device as an NVIDIA candidate.
        Discovery only assembles mounts; the backend must still run its CUDA probe.
        """
        cuda = Path("/usr/lib/wsl/lib/libcuda.so.1")
        if _nvidia_cards() or (Path("/dev/dxg").exists() and (cuda.exists() or cuda.is_symlink())):
            return cls.discover("all")
        return None

    @classmethod
    def discover(cls, selection):
        SandboxPolicy(gpus=selection)  # Share CLI syntax, never interpolate shell text.
        if Path("/dev/dxg").exists():
            if selection != "all":
                raise ValueError(
                    "WSL2 native GPU 仅支持 --sandbox-gpus all；/dev/dxg 无法按显卡拆分授权"
                )
            if not Path("/usr/lib/wsl/lib/libcuda.so.1").is_file():
                raise ValueError(
                    "WSL2 缺少 /usr/lib/wsl/lib/libcuda.so.1；请检查 Windows NVIDIA 驱动"
                )
            gpu = cls("wsl2", selection, (DeviceNode.read(Path("/dev/dxg")),))
        else:
            cards = _nvidia_cards()
            if not cards:
                raise ValueError("未找到 NVIDIA GPU 设备；native CUDA 不会回退 CPU 或未隔离执行")
            nodes = [Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm"), *cards]
            proc = Path("/proc/driver/nvidia")
            if not proc.is_dir() or proc.is_symlink():
                raise ValueError("缺少 NVIDIA 驱动信息 /proc/driver/nvidia")
            gpu = cls("nvidia", selection, tuple(DeviceNode.read(p) for p in nodes), (proc,))
        # Only recognize conventional system installations, never inherit CUDA_HOME
        # or LD_LIBRARY_PATH from the host (they may expose private/workspace files).
        for candidate in (Path("/usr/local/cuda"), Path("/opt/cuda")):
            root = candidate.resolve()
            if (
                root != Path("/usr")
                and root != Path("/opt")
                and any(root.is_relative_to(p) for p in (Path("/usr"), Path("/opt")))
                and (root / "bin/nvcc").is_file()
            ):
                gpu = replace(gpu, cuda_home=root, read_paths=(*gpu.read_paths, root))
                break
        return gpu

    def select(self, inventory):
        """nvidia-smi index is NOT necessarily the device-node minor number."""
        rows = []
        for line in inventory.splitlines():
            fields = [s.strip() for s in line.split(",")]
            if (
                len(fields) != 3
                or not fields[0].isdigit()
                or not fields[1].isdigit()
                or not re.fullmatch(
                    r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", fields[2]
                )
            ):
                raise ValueError("无法解析 nvidia-smi GPU 索引、设备号和 UUID")
            rows.append([fields[0], fields[1], "GPU-" + fields[2][4:].lower()])
        selected = [row for row in rows if self.requested.lower() in (row[0], row[2].lower())]
        if len(selected) != 1:
            raise ValueError(f"找不到唯一的 NVIDIA GPU：{self.requested}")
        _, minor, uuid = selected[0]
        path = Path(f"/dev/nvidia{int(minor)}")
        if not any(node.path == path for node in self.devices):
            raise ValueError(f"所选 GPU 缺少设备节点：{path}")
        return replace(
            self,
            visible_uuid=uuid,
            devices=tuple(
                node
                for node in self.devices
                if node.path == path or node.path.name in {"nvidiactl", "nvidia-uvm"}
            ),
        )

    def mount_args(self):
        args = []
        for node in self.devices:
            node.verify()
            args.extend(["--dev-bind", str(node.path), str(node.path)])
        return args

    def environment(self, scratch):
        result = {
            "CUDA_CACHE_PATH": str(scratch / "cuda-cache"),
            "TRITON_CACHE_DIR": str(scratch / "triton-cache"),
            "TORCH_EXTENSIONS_DIR": str(scratch / "torch-extensions"),
        }
        libraries = []
        if self.kind == "wsl2":
            libraries.append("/usr/lib/wsl/lib")
        if self.cuda_home:
            result["CUDA_HOME"] = result["CUDA_PATH"] = str(self.cuda_home)
            libraries.append(str(self.cuda_home / "lib64"))
        if libraries:
            result["LD_LIBRARY_PATH"] = ":".join(libraries)
        if self.visible_uuid:
            result["CUDA_VISIBLE_DEVICES"] = self.visible_uuid
        return result
