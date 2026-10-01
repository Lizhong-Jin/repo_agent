"""Release integrity checks shared by the builder, installer and uninstaller."""

import hashlib
import json
import re
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from host_support.archives import archive_path
from host_support.platforms import RELEASE_TARGETS

RELEASE_SCHEMA = 4


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_release(root, *, verify=True):
    root = Path(root)
    manifest = root / "release.json"
    if manifest.is_symlink():
        raise ValueError("发行清单不能是链接")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema") not in (1, 2, 3, RELEASE_SCHEMA)
        or data.get("name") != "repo-agent"
        or not isinstance(data.get("version"), str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*", data.get("version", ""))
        or not isinstance(data.get("files"), dict)
    ):
        raise ValueError("无法识别发行清单")
    seen = set()
    target = data.get("target")
    if data["schema"] == RELEASE_SCHEMA and (
        not isinstance(target, str) or target not in RELEASE_TARGETS
    ):
        raise ValueError("无法识别发行平台")
    windows = data["schema"] == 3 or target == "windows-x86_64"
    if windows:
        if target != "windows-x86_64":
            raise ValueError("无法识别 Windows 发行平台")
        from host_support.windows_files import validate_snapshot_names

        validate_snapshot_names(data["files"])
    for name, expected in data["files"].items():
        relative = archive_path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or name == "release.json"
            or str(relative) in seen
            or str(relative) != name
            or not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            raise ValueError("发行清单含无效路径或哈希")
        seen.add(str(relative))
        path = root / relative
        if verify and (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
            or digest(path) != expected
        ):
            raise ValueError(f"发行文件缺失或校验失败：{name}")
    wheel = data.get("wheel")
    if not isinstance(wheel, str):
        raise ValueError("发行包缺少 wheel")
    required = {
        wheel,
        ".env.example",
        "pyproject.toml",
        "requirements-core.lock",
        "requirements-lsp.lock",
        "uv.lock",
    }
    # Schemas 1–3 retain the original cli/ layout. Schema 4 makes the service
    # package move explicit so a damaged new bundle cannot pass as a legacy one.
    package = "installer" if data["schema"] == RELEASE_SCHEMA else "cli"
    required.update({f"{package}/setup.py", f"{package}/release_install.py"})
    if data["schema"] == RELEASE_SCHEMA:
        required.update(
            {
                "installer/__init__.py",
                "installer/_bootstrap.py",
                "installer/release_manifest.py",
                "configuration/__init__.py",
                "configuration/storage.py",
                "host_support/__init__.py",
            }
        )
    if data["schema"] == 1:
        required.update({"install.sh", "install-release.sh", "uninstall.sh"})
    elif not windows:
        required.update(
            {
                "install-release.sh",
                "scripts/installer-entry.sh",
                "scripts/bootstrap-python.sh",
                "runtime/python.lock",
                f"{package}/uninstall.py",
            }
        )
    else:
        required.update(
            {
                "install_release.ps1",
                "runtime/python/python.exe",
                "runtime/python.lock",
                "runtime/target",
                f"{package}/uninstall.py",
            }
        )
        if any(
            name.endswith((".sh", ".ps1")) and name != "install_release.ps1"
            for name in data["files"]
            if "/" not in name or name.startswith("scripts/")
        ):
            raise ValueError("Windows 发行包仅允许 install_release.ps1 安装入口")
    if (
        not isinstance(wheel, str)
        or not wheel.startswith("wheels/")
        or not wheel.endswith(".whl")
        or not required <= data["files"].keys()
    ):
        raise ValueError("发行包缺少必要文件")
    rust_wheel = data.get("rust_wheel")
    if rust_wheel is not None and (
        target not in {"linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64"}
        or not isinstance(rust_wheel, str)
        or not re.fullmatch(r"wheels/rust_backend-[A-Za-z0-9_.+-]+\.whl", rust_wheel)
        or rust_wheel not in data["files"]
    ):
        raise ValueError("发行包 Rust 扩展记录无效")
    return data
