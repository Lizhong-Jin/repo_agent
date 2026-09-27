"""Host identity and explicit release targets; never infer the execution guest."""

import platform
from dataclasses import dataclass

SYSTEMS = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
ARCHES = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}


@dataclass(frozen=True)
class PlatformInfo:
    system: str
    architecture: str

    @classmethod
    def detect(cls):
        return cls.from_names(platform.system(), platform.machine())

    @classmethod
    def from_names(cls, system, machine):
        try:
            return cls(SYSTEMS[system], ARCHES[machine.lower()])
        except KeyError as error:
            raise ValueError(f"Unsupported platform: {system}/{machine}") from error

    @property
    def target(self):
        return f"{self.system}-{self.architecture}"


@dataclass(frozen=True)
class ReleaseTarget:
    name: str
    runtime_python: str
    archive_suffix: str = ".tar.gz"
    runtime_archive_suffix: str = ".tar.gz"

    def wheel_platforms(self):
        system, arch = self.name.split("-")
        if system == "windows":
            return ["win_amd64"]
        if system == "linux":
            arch = "aarch64" if arch == "arm64" else arch
            return [f"manylinux_2_{minor}_{arch}" for minor in range(28, 16, -1)] + [
                f"manylinux2014_{arch}"
            ]
        # Build-only dependency; bootstrapping never imports packaging.
        from packaging.tags import mac_platforms

        return list(mac_platforms((11, 0) if arch == "arm64" else (10, 15), arch))


RELEASE_TARGETS = {
    f"{system}-{arch}": ReleaseTarget(f"{system}-{arch}", "python/bin/python3")
    for system in ("macos", "linux")
    for arch in ("arm64", "x86_64")
}
RELEASE_TARGETS["windows-x86_64"] = ReleaseTarget(
    "windows-x86_64", "python/python.exe", archive_suffix=".zip"
)


def release_target(name):
    try:
        return RELEASE_TARGETS[name]
    except KeyError as error:
        raise ValueError(f"不支持的构建平台：{name}") from error
