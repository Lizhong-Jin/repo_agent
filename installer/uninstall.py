"""Remove only journaled installation resources; never import the agent or its dependencies."""

import argparse
import json
import os
import shutil
import subprocess
from contextlib import ExitStack
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from configuration.storage import backups, config_lock
from host_support.locking import file_lock
from host_support.paths import installed_command, public_command, user_bin_dir

from .install_transaction import TRANSACTION
from .installation import (
    COMMANDS,
    MANIFEST,
    load_record,
    read_record,
    registry_dir,
    save_record,
)
from .paths import installation_root


def other_installations(data: dict) -> tuple[list[dict], bool]:
    others = []
    uncertain = False
    directories = {registry_dir(), Path(data["registry"]).parent}
    for directory in directories:
        for path in directory.glob("*.json"):
            if path == Path(data["registry"]):
                continue
            try:
                item = read_record(path)
                if item["status"] != "uninstalled":
                    others.append(item)
            except (OSError, ValueError):
                uncertain = True
    # Older installations have no registry. Public command links still reveal their use.
    bins = {user_bin_dir().resolve()}
    bins.update(Path(path) for path in data.get("bin_dirs", []))
    bins.update(Path(item["path"]).parent for item in data["commands"])
    for directory in bins:
        for name in COMMANDS:
            command = public_command(directory, name)
            if os.name == "nt":
                from host_support.windows_install import command_state
                try:
                    target = command_state(command)
                    uncertain |= target is not None and target != str(installed_command(Path(data["root"]), name))
                except ValueError:
                    uncertain = True
                continue
            if command.is_symlink():
                target = Path(os.path.abspath(command.parent / os.readlink(command)))
                if target != installed_command(Path(data["root"]), name):
                    uncertain = True
            elif command.exists():
                uncertain = True
    return others, uncertain


def unchanged_parent(path: Path) -> bool:
    return path.is_absolute() and path.parent.resolve() == path.parent


def unlink_owned_command(item: dict, root: Path, *, dry_run: bool) -> bool:
    path, target = Path(item["path"]), Path(item["target"])
    name = path.stem if os.name == "nt" else path.name
    if name not in COMMANDS or target != installed_command(root, name):
        raise ValueError("安装记录的命令路径无效")
    if os.name == "nt":
        from host_support.windows_install import command_receipt, command_state
        try:
            actual = command_state(path)
        except ValueError:
            actual = "modified"
        if actual is None:
            return True
        if not unchanged_parent(path) or actual != str(target):
            print(f"保留已修改或改指向的命令：{path}")
            return False
        print(f"{'将删除' if dry_run else '删除'}命令：{path}")
        if not dry_run:
            path.unlink()
            command_receipt(path).unlink()
        return True
    if not path.exists() and not path.is_symlink():
        return True
    if not unchanged_parent(path) or not path.is_symlink():
        print(f"保留非原命令链接：{path}")
        return False
    actual = Path(os.path.abspath(path.parent / os.readlink(path)))
    if actual != target:
        print(f"保留已改指向的命令：{path}")
        return False
    print(f"{'将删除' if dry_run else '删除'}命令链接：{path}")
    if not dry_run:
        path.unlink()
    return True


def remove_venv(data: dict, *, dry_run: bool) -> bool:
    path = Path(data["root"]) / ".venv"
    if not path.exists() and not path.is_symlink():
        return True
    if path.is_symlink() or not path.is_dir() or not unchanged_parent(path):
        print(f"保留无法确认归属的虚拟环境：{path}")
        return False
    marker = path / MANIFEST
    try:
        if marker.is_symlink():
            raise ValueError("ownership marker is a symlink")
        owner = json.loads(marker.read_text(encoding="utf-8"))
        info = path.stat()
        if owner != {"id": data["id"], "root": data["root"]} or data.get("venv") != {
            "device": info.st_dev,
            "inode": info.st_ino,
        }:
            raise ValueError("ownership changed")
    except (OSError, ValueError):
        print(f"保留没有匹配安装标记的虚拟环境：{path}")
        return False
    print(f"{'将删除' if dry_run else '删除'}虚拟环境：{path}")
    if not dry_run:
        shutil.rmtree(path)
    return True


def clean_shell(data: dict, others: list[dict], uncertain: bool, *, dry_run: bool) -> None:
    if os.name == "nt":
        from host_support.windows_install import apply_path_change
        for change in reversed(data.get("windows_path", [])):
            bin_dir = Path(change["bin_dir"])
            shared = uncertain or bin_dir == user_bin_dir().resolve() or any(
                Path(command["path"]).parent == bin_dir
                for other in others for command in other["commands"]
            )
            if shared:
                print(f"保留共享用户 PATH：{bin_dir}")
            elif dry_run:
                print(f"将恢复安装前的用户 PATH：{bin_dir}")
            else:
                try:
                    apply_path_change(change, restore=True)
                except ValueError as error:
                    print(f"保留已修改的用户 PATH：{error}")
    for item in data["shell"]:
        path, bin_dir = Path(item["path"]), Path(item["bin_dir"])
        if not path.exists() and not path.is_symlink():
            continue
        shared = bin_dir == user_bin_dir().resolve() or uncertain
        shared |= any(
            Path(command["path"]).parent == bin_dir
            for other in others
            for command in other["commands"]
        )
        if bin_dir.exists():
            # Ignore this installation's own links in dry-run so its plan matches execution.
            owned = {Path(command["path"]) for command in data["commands"]}
            shared |= any(path not in owned for path in bin_dir.iterdir())
        if shared:
            print(f"保留共享 PATH 配置：{path}")
            continue
        if path.is_symlink() or not unchanged_parent(path):
            print(f"保留已改变位置的 shell 配置：{path}")
            continue
        text = path.read_text(encoding="utf-8")
        block = item["block"]
        if not isinstance(block, str) or f"# >>> Repo Agent {data['id']}\n" not in block:
            raise ValueError("安装记录的 shell 配置块无效")
        if block not in text:
            print(f"保留已修改的 shell 配置：{path}")
            continue
        print(f"{'将移除' if dry_run else '移除'}安装器添加的 shell 配置块：{path}")
        if not dry_run:
            remaining = text.replace(block, "", 1)
            if item.get("created") and not remaining:
                path.unlink()
            else:
                path.write_text(remaining, encoding="utf-8")


def remove_config(data: dict, others: list[dict], uncertain: bool, *, dry_run: bool) -> None:
    path = Path(data["config"])
    if uncertain or any(other["config"] == str(path) for other in others):
        print(f"保留其他安装仍使用或归属不明确的用户配置：{path}")
        return
    if path.is_symlink() or (path.exists() and not path.is_file()) or not unchanged_parent(path):
        print(f"保留类型或位置已改变的配置：{path}")
        return
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(config_lock(path))
        for item in [path, *backups(path)]:
            if item.exists():
                print(f"{'将删除' if dry_run else '删除'}用户配置或备份（含 API Key）：{item}")
                if not dry_run:
                    item.unlink()
        preferences = path.with_name("thinking.json")
        if preferences.is_file() and not preferences.is_symlink():
            print(f"{'将删除' if dry_run else '删除'}模型思考偏好：{preferences}")
            if not dry_run:
                preferences.unlink()
        if not dry_run:
            for directory in (path.parent / ".env.backups", path.parent):
                try:
                    directory.rmdir()
                except OSError:
                    pass


def remove_images(data: dict, others: list[dict], uncertain: bool, *, dry_run: bool) -> bool:
    ok = True
    if not data["images"]:
        print("没有可确认归属的镜像记录，保留现有镜像。")
    for item in data["images"]:
        tag = item["tag"]
        shared = uncertain or any(
            (other.get("uses_default_image") and tag == "repo-agent-sandbox:v1")
            or any(image["id"] == item["id"] or image["tag"] == tag for image in other["images"])
            for other in others
        )
        if shared:
            print(f"保留其他安装可能使用的镜像：{tag}")
            continue
        try:
            daemon = subprocess.run(
                ["docker", "info", "--format", "{{.ID}}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            if daemon != item["daemon"]:
                print(f"保留镜像：当前 Docker 主机与构建时不同（{tag}）")
                continue
            inspect = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if inspect.returncode != 0:
                print(f"镜像不存在或无法检查，未删除：{tag}")
                continue
            if inspect.stdout.strip() != item["id"]:
                print(f"保留已被其他构建替换的镜像：{tag}")
                continue
            containers = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"ancestor={item['id']}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if containers.stdout.strip():
                print(f"保留仍有容器使用的镜像：{tag}")
                continue
            print(f"{'将移除' if dry_run else '移除'}镜像标签：{tag}")
            if not dry_run:
                subprocess.run(["docker", "image", "rm", tag], check=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            print(f"镜像清理失败，已保留记录，可在 Docker 恢复后重试：{tag}")
            ok = False
    return ok


def _uninstall(
    root: Path, *, dry_run: bool = False, purge: bool = False, remove_image: bool = False
) -> bool:
    root = root.resolve(strict=True)
    data = load_record(root)
    if data is None:
        print("未找到安装记录；未删除任何文件。旧版本请先重新安装以登记归属。")
        return True
    others, uncertain = other_installations(data)
    ok = True
    for command in data["commands"]:
        # Redirected commands may belong to an older, unregistered installation.
        uncertain |= not unlink_owned_command(command, root, dry_run=dry_run)
    clean_shell(data, others, uncertain, dry_run=dry_run)
    if purge:
        remove_config(data, others, uncertain, dry_run=dry_run)
    else:
        print(f"保留用户配置：{data['config']}（--purge 可清除未共享的配置）")
    if remove_image:
        ok = remove_images(data, others, uncertain, dry_run=dry_run)
    ok = remove_venv(data, dry_run=dry_run) and ok
    if not dry_run:
        data["status"] = "uninstalled" if ok else "cleanup-pending"
        # Retain a registry receipt for repeat uninstall and later --purge/--remove-image.
        save_record(data, local=not ok)
        if ok:
            marker = root / MANIFEST
            if marker.exists() and not marker.is_symlink():
                marker.unlink()
    print("预览完成，未修改文件。" if dry_run else "卸载处理完成；源码、任务日志和沙箱副本已保留。")
    return ok


def uninstall(root: Path, *, dry_run=False, purge=False, remove_image=False):
    root = root.resolve(strict=True)
    # Reject copied/corrupt journals without creating even lock files.
    if load_record(root) is None:
        return _uninstall(root, dry_run=dry_run, purge=purge, remove_image=remove_image)
    # A preview remains read-only.
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(file_lock(registry_dir().parent / ".maintenance.lock"))
            stack.enter_context(file_lock(root / ".repo-agent-operation.lock"))
        if (root / TRANSACTION).exists():
            entry = "install_release.ps1" if os.name == "nt" else "install-release.sh" if (root / "release.json").is_file() else "install.sh"
            raise ValueError(f"有未完成的安装恢复；请先运行 ./{entry} --recover")
        return _uninstall(root, dry_run=dry_run, purge=purge, remove_image=remove_image)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="卸载当前 Repo Agent 安装，默认保留配置和任务数据")
    parser.add_argument("--agent-home", type=Path, default=installation_root())
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改文件或镜像")
    parser.add_argument(
        "--purge", action="store_true", help="同时删除未被其他安装使用的用户配置和 Key"
    )
    parser.add_argument(
        "--remove-image", action="store_true", help="移除可确认归属、未共享且未使用的镜像"
    )
    args = parser.parse_args(argv)
    try:
        ok = uninstall(
            args.agent_home, dry_run=args.dry_run, purge=args.purge, remove_image=args.remove_image
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"卸载未完成：{type(error).__name__}: {error}\n")
    if not ok:
        parser.exit(1)


if __name__ == "__main__":
    main()
