"""Standard-library-only installation journal, usable before pip or without a venv."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from host_support.paths import app_directory, installed_command, public_command, user_config_path
from host_support.storage import atomic_write

MANIFEST = ".repo-agent-install.json"
COMMANDS = ("repo-agent", "repo-agent-build-sandbox")
DEFAULT_IMAGE = "repo-agent-sandbox:v1"


def registry_dir() -> Path:
    return app_directory("state") / "installations"


def registry_name(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest() + ".json"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, content.encode("utf-8"), prefix=".install-")


def read_record(path: Path, root: Path | None = None) -> dict:
    if path.is_symlink():
        raise ValueError(f"安装记录不能是符号链接：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"无法识别安装记录：{path}")
    required = ("id", "root", "owner_home", "registry", "config", "status")
    if any(not isinstance(data.get(key), str) for key in required):
        raise ValueError(f"安装记录不完整：{path}")
    if len(data["id"]) != 32 or any(c not in "0123456789abcdef" for c in data["id"]):
        raise ValueError(f"安装记录 ID 无效：{path}")
    if root is not None and data["root"] != str(root):
        raise ValueError("安装记录属于其他目录（可能复制或移动过项目）；未修改原安装")
    if data["owner_home"] != str(Path.home().resolve()):
        raise ValueError("安装记录属于其他用户；未修改该安装")
    for key in ("root", "registry", "config"):
        if not Path(data[key]).is_absolute():
            raise ValueError("安装记录含无效路径")
    if Path(data["registry"]).name != registry_name(Path(data["root"])):
        raise ValueError("安装记录的登记路径无效")
    if Path(data["config"]).name != ".env":
        raise ValueError("安装记录的配置路径无效")
    if not all(isinstance(data.get(key), list) for key in ("commands", "shell", "images")):
        raise ValueError("安装记录的资源列表无效")
    if data["status"] not in {"installing", "installed", "uninstalled", "cleanup-pending"}:
        raise ValueError("安装记录状态无效")
    if not isinstance(data.get("windows_path", []), list):
        raise ValueError("Windows PATH 记录列表无效")
    for item in data.get("windows_path", []):
        from host_support.windows_install import validate_path_change
        validate_path_change(item)
    for item in data["commands"]:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) for key in ("path", "target")
        ):
            raise ValueError("安装记录的命令条目无效")
        path = Path(item["path"])
        if (
            not path.is_absolute()
            or path != public_command(path.parent, path.stem if os.name == "nt" else path.name)
            or (path.stem if os.name == "nt" else path.name) not in COMMANDS
            or Path(item["target"]) != installed_command(Path(data["root"]), path.stem if os.name == "nt" else path.name)
        ):
            raise ValueError("安装记录的命令路径无效")
    for item in data["shell"]:
        if (
            not isinstance(item, dict)
            or not all(isinstance(item.get(key), str) for key in ("path", "bin_dir", "block"))
            or not Path(item["path"]).is_absolute()
            or not Path(item["bin_dir"]).is_absolute()
            or f"# >>> Repo Agent {data['id']}\n" not in item["block"]
        ):
            raise ValueError("安装记录的 shell 条目无效")
    for item in data["images"]:
        if (
            not isinstance(item, dict)
            or not all(
                isinstance(item.get(key), str) and item[key] for key in ("tag", "id", "daemon")
            )
            or item["tag"].startswith("-")
            or not item["id"].startswith("sha256:")
        ):
            raise ValueError("安装记录的镜像条目无效")
    directories = data.get("bin_dirs", [])
    if not isinstance(directories, list) or any(
        not isinstance(path, str) or not Path(path).is_absolute() for path in directories
    ):
        raise ValueError("安装记录的命令目录无效")
    return data


def load_record(root: Path) -> dict | None:
    local = root / MANIFEST
    if local.exists() or local.is_symlink():
        return read_record(local, root)
    registered = registry_dir() / registry_name(root)
    if registered.exists() or registered.is_symlink():
        return read_record(registered, root)
    return None


def save_record(data: dict, *, local: bool = True) -> None:
    # Registry first: it also provides recovery if the repository marker is missing.
    write_json(Path(data["registry"]), data)
    if local:
        write_json(Path(data["root"]) / MANIFEST, data)


def begin_install(root: Path) -> dict:
    root = root.resolve(strict=True)
    try:
        data = load_record(root)
    except ValueError:
        # A clone can contain a copied journal. Start a new identity, never touch its origin.
        copied = root / MANIFEST
        if copied.is_symlink():
            raise
        previous = json.loads(copied.read_text(encoding="utf-8"))
        if not isinstance(previous, dict) or previous.get("root") == str(root):
            raise
        data = None
    if (
        data is not None
        and data["status"] == "uninstalled"
        and data["config"] != str(user_config_path())
    ):
        data = None  # An explicit new configuration after uninstall starts a new ownership record.
    if data is None:
        data = {
            "version": 1,
            "id": uuid4().hex,
            "root": str(root),
            "owner_home": str(Path.home().resolve()),
            "registry": str(registry_dir() / registry_name(root)),
            "config": str(user_config_path()),
            "config_created": False,
            "bin_dirs": [],
            "commands": [],
            "shell": [],
            "images": [],
            "status": "installing",
            "uses_default_image": True,
        }
    else:
        # Keep the original configured location so changing environment variables cannot
        # silently transfer deletion rights to a different file during a reinstall.
        if data["config"] != str(user_config_path()):
            raise ValueError("配置目录已改变；请先卸载原安装，或恢复原配置目录后重试")
        data["status"] = "installing"
    save_record(data)
    return data


def prepare_venv(data: dict) -> None:
    path = Path(data["root"]) / ".venv"
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(".venv 必须是安装目录内的普通目录，不能是符号链接")
    existing_owner = None
    marker = path / MANIFEST
    if marker.is_file() and not marker.is_symlink():
        existing_owner = json.loads(marker.read_text(encoding="utf-8"))
    if (
        path.exists()
        and any(path.iterdir())
        and not (path / "pyvenv.cfg").is_file()
        and existing_owner != {"id": data["id"], "root": data["root"]}
    ):
        raise ValueError("已有 .venv 不是可识别的虚拟环境，请先手动移走该目录")
    path.mkdir(mode=0o700, exist_ok=True)
    owner = {"id": data["id"], "root": data["root"]}
    write_json(path / MANIFEST, owner)
    info = path.stat()
    data["venv"] = {"device": info.st_dev, "inode": info.st_ino}
    save_record(data)


def record_image(root: Path, tag: str) -> None:
    try:
        data = load_record(root)
        if data is None or data["status"] == "uninstalled":
            return
        image = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        daemon = subprocess.run(
            ["docker", "info", "--format", "{{.ID}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        if not image.startswith("sha256:") or not daemon:
            raise ValueError("无法确认镜像或 Docker 主机标识")
        item = {"tag": tag, "id": image, "daemon": daemon}
        data["images"] = [entry for entry in data["images"] if entry["tag"] != tag] + [item]
        save_record(data)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("镜像已构建，但未能记录镜像归属；自动卸载会保留该镜像。")


def main() -> None:
    parser = argparse.ArgumentParser(description="记录安装并准备项目虚拟环境")
    parser.add_argument("--agent-home", type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare_venv(begin_install(args.agent_home))
    except (OSError, ValueError) as error:
        parser.exit(1, f"安装准备失败：{error}\n")


if __name__ == "__main__":
    main()
