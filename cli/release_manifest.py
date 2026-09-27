"""Release integrity checks shared by the builder, installer and uninstaller."""

import hashlib
import json
import re
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()

from host_support.archives import archive_path


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_release(root, *, verify=True):
    root = Path(root)
    manifest = root / "release.json"
    if manifest.is_symlink():
        raise ValueError("发行清单不能是链接")
    data = json.loads(manifest.read_text())
    if (
        not isinstance(data, dict)
        or data.get("schema") not in (1, 2)
        or data.get("name") != "repo-agent"
        or not isinstance(data.get("version"), str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*", data.get("version", ""))
        or not isinstance(data.get("files"), dict)
    ):
        raise ValueError("无法识别发行清单")
    seen = set()
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
        "install-release.sh",
        "cli/setup.py",
        "cli/release_install.py",
        ".env.example",
        "pyproject.toml",
        "requirements-core.lock",
        "requirements-lsp.lock",
        "uv.lock",
    }
    if data["schema"] == 1:
        required.update({"install.sh", "uninstall.sh"})
    else:
        required.update(
            {
                "scripts/installer-entry.sh",
                "scripts/bootstrap-python.sh",
                "runtime/python.lock",
                "cli/uninstall.py",
            }
        )
    if (
        not isinstance(wheel, str)
        or not wheel.startswith("wheels/")
        or not wheel.endswith(".whl")
        or not required <= data["files"].keys()
    ):
        raise ValueError("发行包缺少必要文件")
    return data
