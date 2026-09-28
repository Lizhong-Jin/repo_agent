"""Fresh mount-namespace observations for native policy diagnostics and validation."""

import re
import sys
from dataclasses import dataclass
from pathlib import Path


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


@dataclass(frozen=True)
class Mount:
    mount_id: str
    path: str
    filesystem: str
    source: str
    options: tuple[str, ...]


class MountTable:
    def __init__(self, text):
        self.text = text
        self.mounts = {}
        for line in text.splitlines():
            before, after = line.split(" - ", 1)
            fields, fs = before.split(), after.split()
            mount = Mount(
                fields[0],
                _unescape(fields[4]),
                fs[0],
                _unescape(fs[1]),
                tuple(fields[5].split(",")),
            )
            self.mounts[mount.path] = mount
        self.ordered = sorted(self.mounts.values(), key=lambda mount: len(mount.path), reverse=True)

    @classmethod
    def read(cls):
        return cls(Path("/proc/self/mountinfo").read_text() if sys.platform == "linux" else "")

    def containing(self, path):
        path = str(path)
        return next(
            (
                mount
                for mount in self.ordered
                if mount.path == "/" or path == mount.path or path.startswith(mount.path + "/")
            ),
            None,
        )

    def verify(self):
        if type(self).read().text != self.text:
            raise ValueError("Linux native 扫描期间挂载布局发生变化，拒绝使用旧策略")
