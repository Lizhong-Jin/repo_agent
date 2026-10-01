"""Select optional binary companions using the release's Python and platform tags."""

import shutil
import subprocess
import sys
from pathlib import Path

from packaging.tags import cpython_tags
from packaging.utils import parse_wheel_filename

from host_support.platforms import PlatformInfo, release_target
from installer.rust_extension import RUST_TARGETS, build_rust_wheel, rust_version


def select_rust_wheel(directory, target, python_version, version):
    if directory is None or target not in RUST_TARGETS:
        return None
    python = tuple(int(part) for part in python_version.split(".")[:2])
    supported = {
        tag: index
        for index, tag in enumerate(
            cpython_tags(python_version=python, platforms=release_target(target).wheel_platforms())
        )
        if tag.abi == "abi3"
    }
    candidates = []
    # Accept a versioned artifact root or an explicitly supplied flat wheelhouse.
    directory = Path(directory)
    wheels = set(directory.glob("rust_backend-*.whl"))
    wheels.update((directory / version).glob("rust_backend-*.whl"))
    for wheel in sorted(wheels):
        name, candidate_version, _, tags = parse_wheel_filename(wheel.name)
        if name != "rust-backend" or str(candidate_version) != version:
            continue
        matching = tags & supported.keys()
        if matching:
            candidates.append((min(supported[tag] for tag in matching), wheel))
    return min(candidates)[1] if candidates else None


def prepare_rust_wheels(root, records, output, *, wheelhouse=None, offline=False, required=False):
    """Use prebuilt wheels first; only attempt a local build for the actual host target."""
    version = rust_version(root)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selected = {}
    for target, record in records.items():
        if target not in RUST_TARGETS:
            continue
        local = root / "rust_wheels"
        wheel = select_rust_wheel(wheelhouse, target, record["version"], version)
        if wheel is None:
            wheel = select_rust_wheel(local, target, record["version"], version)
        if wheel is None and target == PlatformInfo.detect().target:
            try:
                build_rust_wheel(
                    root,
                    sys.executable,
                    local,
                    offline=offline,
                    wheelhouse=wheelhouse,
                    compatibility="manylinux_2_28" if target.startswith("linux-") else None,
                )
                wheel = select_rust_wheel(local, target, record["version"], version)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"[WARN] 本机 Rust 扩展构建未完成：{error}")
        if wheel is None:
            message = f"{target} 缺少符合发行平台要求的 Rust wheel；可通过 --rust-wheelhouse 提供"
            if required:
                raise ValueError(message)
            print(f"[WARN] {message}；该发行包仅提供 Python 扫描器。")
            continue
        destination = output / wheel.name
        if wheel.resolve() != destination.resolve():
            shutil.copy2(wheel, destination)
        selected[target] = destination
    return selected
