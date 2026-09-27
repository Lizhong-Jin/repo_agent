"""Restrict WSL's driver-store mount to the CUDA adapters' own packages.

No recursive driver-store discovery and no cross-call directory-content cache.
Adapter discovery runs in a sandbox with the entire driver store hidden.
"""

import json
import stat
from dataclasses import dataclass
from pathlib import Path

from tools._internal.file_policy import is_protected_name

from .linux_mounts import MountTable

DRIVER_STORE = Path("/usr/lib/wsl/drivers")
CUDA_COMPONENTS = ("libcuda.so.1.1", "libcuda_loader.so", "libcuda.so.1")


@dataclass
class WSLDriverStore:
    root: Path
    identity: tuple[int, int]
    mount_id: str
    packages: tuple[Path, ...] = ()
    package_identities: tuple[tuple[int, int], ...] = ()

    @classmethod
    def detect(cls):
        table = MountTable.read()
        mount = table.mounts.get(str(DRIVER_STORE))
        if mount is None or mount.filesystem != "9p" or mount.source != "drivers":
            return None
        info = DRIVER_STORE.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("WSL 驱动存储必须是实际目录")
        return cls(DRIVER_STORE, (info.st_dev, info.st_ino), mount.mount_id)

    def select(self, output):
        try:
            report = json.loads(output)
            paths = report["driver_store_paths"]
            if not isinstance(paths, list) or not paths or len(paths) > 256:
                raise ValueError("invalid adapter count")
            selected = set()
            for value in paths:
                if not isinstance(value, str):
                    raise ValueError("invalid driver path")
                path = Path(value)
                # dxcore returns immediate package directories, never arbitrary
                # paths, nested paths or user-provided CUDA_HOME values.
                if (not path.is_absolute() or ".." in path.parts or path.parent != self.root
                        or is_protected_name(path.name) or path.resolve(strict=True) != path):
                    raise ValueError("unexpected driver package path")
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("driver package is not a directory")
                if any((path / name).is_file() for name in CUDA_COMPONENTS):
                    selected.add(path)
            if not selected:
                raise ValueError("no CUDA driver packages")
            packages = tuple(sorted(selected))
            identities = tuple((info.st_dev, info.st_ino) for info in
                               (path.lstat() for path in packages))
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise ValueError("无法确认 WSL CUDA 驱动包；不会暴露整个驱动存储或回退 CPU") from error
        self.packages, self.package_identities = packages, identities

    def verify(self, table):
        mount = table.mounts.get(str(self.root))
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.identity
                or mount is None or mount.mount_id != self.mount_id
                or mount.filesystem != "9p" or mount.source != "drivers"):
            raise ValueError("WSL 驱动挂载发生变化，请重启会话")
        for path, identity in zip(self.packages, self.package_identities, strict=True):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity
                    or path.resolve(strict=True) != path):
                raise ValueError("WSL 驱动包发生变化，请重启会话")

    def views(self, read_paths):
        views = set()
        for path in read_paths:
            resolved = path.resolve(strict=True)
            if self.root.is_relative_to(resolved):
                views.add(path / self.root.relative_to(resolved))
            elif resolved.is_relative_to(self.root):
                raise ValueError("不能把 WSL 驱动存储内部路径作为通用运行环境")
        for view in views:
            if view.is_symlink() or view.resolve(strict=True) != self.root:
                raise ValueError("WSL 驱动别名发生变化，拒绝执行")
        return tuple(sorted(views))

    def mount_args(self, views):
        args = []
        for view in views:
            # An empty synthetic directory replaces the original broad read-only
            # mount. Only selected, separately scanned packages are restored.
            args.extend(["--tmpfs", str(view)])
            for package in self.packages:
                args.extend(["--ro-bind", str(package), str(view / package.name)])
            args.extend(["--remount-ro", str(view)])
        return args
