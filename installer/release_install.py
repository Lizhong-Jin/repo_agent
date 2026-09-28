"""Install a verified release bundle into a user-owned version directory (stdlib only)."""

import argparse
import os
import shutil
import tarfile
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from host_support.locking import file_lock
from host_support.paths import app_directory, user_bin_dir

from . import setup, uninstall
from .paths import extract_files
from .release_manifest import digest, read_release


def locate_release_root(extracted):
    """Accept legacy flat archives or exactly one enclosing release directory."""
    if (extracted / "release.json").exists() or (extracted / "release.json").is_symlink():
        return extracted  # read_release performs the full integrity check afterwards.
    children = list(extracted.iterdir())
    if len(children) == 1:
        root = children[0]
        if root.is_dir() and not root.is_symlink() and (root / "release.json").is_file():
            return root
    raise ValueError(
        "无法定位发行包：需要根目录中的 release.json，或唯一顶层文件夹内的 release.json"
    )


def prepare_release(bundle, destination):
    release = read_release(bundle)
    if destination.is_symlink():
        raise ValueError("发行版安装目录不能是链接")
    if destination.exists():
        previous = read_release(destination)
        if previous != release:
            raise ValueError("同版本目录已有不同的发行内容；请使用新版本号或先卸载原发行版")
        return release
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        for name in [*release["files"], "release.json"]:
            source, target = bundle / name, staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        read_release(staging)
        staging.rename(destination)
    return release


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装独立 Repo Agent 发行版，不依赖下载/源码目录")
    parser.add_argument("--archive", type=Path, help="发行 tar.gz/ZIP 文件；已解压发行包可省略")
    parser.add_argument("--sha256", help="可选：校验整个发行包的 SHA256")
    parser.add_argument("--data-dir", type=Path, help="用户数据目录，默认 XDG_DATA_HOME/repo-agent")
    parser.add_argument("--bin-dir", type=Path, default=user_bin_dir())
    parser.add_argument("--mode", choices=["native", "docker", "local"])
    parser.add_argument("--languages", default="all")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--with-toolchains", action="store_true")
    choice.add_argument("--skip-toolchains", action="store_true")
    parser.add_argument("--skip-sandbox", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--no-path", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--recover", action="store_true")
    operation.add_argument("--uninstall", action="store_true", help="卸载该发行版本，默认保留配置")
    parser.add_argument("--dry-run", action="store_true", help="预览卸载")
    parser.add_argument("--purge", action="store_true", help="卸载时清理未共享的配置")
    parser.add_argument("--remove-image", action="store_true", help="卸载时清理归属明确的镜像")
    args = parser.parse_args(argv)
    if not args.uninstall and (args.dry_run or args.purge or args.remove_image):
        parser.error("--dry-run、--purge、--remove-image 需要 --uninstall")
    try:
        with ExitStack() as stack:
            bundle = Path(__file__).resolve().parents[1]
            if args.archive:
                archive = args.archive.expanduser().resolve(strict=True)
                if args.sha256 and digest(archive) != args.sha256.lower():
                    raise ValueError("发行包 SHA256 校验失败")
                bundle = Path(
                    stack.enter_context(tempfile.TemporaryDirectory(prefix="repo-agent-release-"))
                )
                extract_files(archive, bundle)
                bundle = locate_release_root(bundle)
            elif args.sha256:
                raise ValueError("--sha256 需要同时提供 --archive")
            release = read_release(bundle, verify=not args.uninstall)
            data_dir = args.data_dir
            if data_dir is None:
                # Recovery/reinstall from an installed version keeps a custom data directory.
                if (
                    not args.archive
                    and bundle.parent.name == "versions"
                    and bundle.name == release["version"]
                ):
                    data_dir = bundle.parent.parent
                else:
                    data_dir = app_directory("data")
            base = data_dir.expanduser().resolve()
            if (base / "versions").is_symlink():
                raise ValueError("versions 目录不能是链接")
            target = base / "versions" / release["version"]
            if target.is_symlink():
                raise ValueError("发行版安装目录不能是链接")
            if args.uninstall:
                # Never stage a new payload or depend on a working venv to uninstall.
                # The uninstall journal checks ownership even if release files are damaged.
                if not target.exists():
                    print(f"未找到已安装版本：{target}；未删除任何文件。")
                    return
                forwarded = ["--agent-home", str(target)]
                for name in ("dry_run", "purge", "remove_image"):
                    if getattr(args, name):
                        forwarded.append("--" + name.replace("_", "-"))
                uninstall.main(forwarded)
                return
            parent = target
            while not parent.exists():
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
                raise ValueError(f"发行版安装目录不可写：{parent}")
            if target.exists() and read_release(target) != release:
                raise ValueError("同版本目录已有不同的发行内容；请使用新版本号或先卸载原发行版")
            forwarded = [
                "--agent-home",
                str(bundle if args.check else target),
                "--bin-dir",
                str(args.bin_dir),
                "--languages",
                args.languages,
            ]
            if args.wheelhouse:
                forwarded += ["--wheelhouse", str(args.wheelhouse.expanduser().resolve())]
            if args.mode:
                forwarded += ["--mode", args.mode]
            for name in (
                "offline",
                "with_toolchains",
                "skip_toolchains",
                "skip_sandbox",
                "no_path",
                "check",
                "recover",
            ):
                if getattr(args, name):
                    forwarded.append("--" + name.replace("_", "-"))
            print(f"发行版本：{release['version']}；安装位置：{target}", flush=True)
            if args.check:
                # Metadata/platform checks do not create installation directories or locks.
                setup.main(forwarded)
                return
            stack.enter_context(file_lock(base / ".release-install.lock"))
            # Confirm takeover before creating any installed version files.
            approved = None
            if not args.recover:
                approved = setup.confirm_commands(target, args.bin_dir.expanduser().resolve())
            prepare_release(bundle, target)
            forwarded += ["--bootstrap", "--wheel", str(target / release["wheel"])]
            setup.main(forwarded, approved_commands=approved)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        parser.exit(1, f"发行版{'卸载' if args.uninstall else '安装'}未完成：{error}\n")


if __name__ == "__main__":
    main()
