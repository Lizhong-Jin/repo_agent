"""Install a verified release bundle into a user-owned version directory (stdlib only)."""

import argparse
import os
import shutil
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path

if __package__:
    from . import setup
    from .maintenance import file_lock
    from .paths import extract_files
    from .release_manifest import digest, read_release
else:
    import setup
    from maintenance import file_lock
    from paths import extract_files
    from release_manifest import digest, read_release


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
    parser.add_argument("--archive", type=Path, help="发行 tar.gz 文件；已解压发行包可省略")
    parser.add_argument("--sha256", help="可选：校验整个发行 tar.gz 的 SHA256")
    parser.add_argument("--data-dir", type=Path, help="用户数据目录，默认 XDG_DATA_HOME/repo-agent")
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--mode", choices=["native", "docker", "local"])
    parser.add_argument("--languages", default="all")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--with-toolchains", action="store_true")
    choice.add_argument("--skip-toolchains", action="store_true")
    parser.add_argument("--skip-sandbox", action="store_true")
    parser.add_argument("--no-path", action="store_true")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--recover", action="store_true")
    args = parser.parse_args(argv)
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
            elif args.sha256:
                raise ValueError("--sha256 需要同时提供 --archive")
            release = read_release(bundle)
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
                    data_dir = (
                        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
                        / "repo-agent"
                    )
            base = data_dir.expanduser().resolve()
            if (base / "versions").is_symlink():
                raise ValueError("versions 目录不能是链接")
            target = base / "versions" / release["version"]
            if target.is_symlink():
                raise ValueError("发行版安装目录不能是链接")
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
            if args.mode:
                forwarded += ["--mode", args.mode]
            for name in (
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
    except (OSError, ValueError, tarfile.TarError) as error:
        parser.exit(1, f"发行版安装未完成：{error}\n")


if __name__ == "__main__":
    main()
