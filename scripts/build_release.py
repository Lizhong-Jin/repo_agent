"""Build complete platform releases under version/platform directories."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

from lock_dependencies import export_locks
from prepare_python_bundle import prepare, runtime_records

# This policy module is stdlib-only; setuptools is installed later in the build venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_manifest import (  # noqa: E402
    CONTEXT_ARCHIVE,
    RESOURCE_FILES,
    bootstrap_files,
    check_configuration,
    copy_files,
    source_files,
    verify_release_archive,
    verify_wheel,
)
from host_support.paths import environment_python  # noqa: E402
from host_support.platforms import release_target  # noqa: E402
from installer.release_manifest import RELEASE_SCHEMA, read_release  # noqa: E402


def runtime_archives(targets, source, *, offline):
    """A single file needs one target; a directory contains <target>.tar.gz files."""
    if source is None:
        if offline:
            raise ValueError("离线构建需要 --runtime-archive（单平台文件或多平台目录）")
        return dict.fromkeys(targets)
    source = Path(source).expanduser().resolve(strict=True)
    if source.is_file() and len(targets) != 1:
        raise ValueError("运行时归档为单个文件时必须指定 --target；全平台构建请提供归档目录")
    paths = {target: source / f"{target}{release_target(target).runtime_archive_suffix}" if source.is_dir() else source
             for target in targets}
    for path in paths.values():
        if not path.is_file():
            raise ValueError(f"缺少平台 Python 归档：{path}")
    return paths


def write_release(bundle, output, version, target, wheel_name):
    files = {
        path.relative_to(bundle).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "schema": RELEASE_SCHEMA,
        "target": target,
        "name": "repo-agent",
        "version": version,
        "wheel": "wheels/" + wheel_name,
        "files": files,
    }
    (bundle / "release.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    # Fail before replacing an existing artifact if bootstrap files are incomplete.
    read_release(bundle)
    directory = output / version / target
    directory.mkdir(parents=True, exist_ok=True)
    release_directory = f"repo-agent-{version}-{target}"
    destination = directory / f"{release_directory}{release_target(target).archive_suffix}"
    fd, temporary_archive = tempfile.mkstemp(prefix=".release-", dir=directory)
    os.close(fd)
    try:
        if release_target(target).archive_suffix == ".zip":
            with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_file():
                        archive.write(path, f"{release_directory}/{path.relative_to(bundle).as_posix()}")
        else:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_file():
                        archive.add(
                            path,
                            arcname=f"{release_directory}/{path.relative_to(bundle).as_posix()}",
                            recursive=False,
                        )
        verify_release_archive(Path(temporary_archive), bundle, prefix=release_directory)
        os.replace(temporary_archive, destination)
    finally:
        Path(temporary_archive).unlink(missing_ok=True)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_name(destination.name + ".sha256").write_text(
        f"{checksum}  {destination.name}\n"
    )
    print(f"发行包：{destination}\nSHA256：{checksum}")
    return destination


def build(root, output, uv, *, target=None, runtime_archive=None, wheelhouse=None, offline=False):
    records = runtime_records(root)
    if target is not None and target not in records:
        raise ValueError(f"不支持的构建平台：{target}")
    targets = [target] if target is not None else list(records)
    if not targets:
        raise ValueError("runtime/python.lock 未声明支持的平台")
    archives = runtime_archives(targets, runtime_archive, offline=offline)
    if offline and not wheelhouse:
        raise ValueError("离线构建需要 --wheelhouse（包含构建依赖及所有目标的运行依赖）")
    check_configuration(root)
    sources = source_files(root)
    bootstrap_files(root)  # Validate every installer input before building.
    export_locks(root, uv, check=True, **({"offline": True} if offline else {}))
    node = root / "dependencies/node"
    declared = json.loads((node / "package.json").read_text())["dependencies"]
    locked = json.loads((node / "package-lock.json").read_text())
    if locked.get("packages", {}).get("", {}).get("dependencies") != declared:
        raise ValueError("npm 锁文件与 package.json 不一致，请重新生成 package-lock.json")
    for name, version in declared.items():
        package = locked["packages"].get("node_modules/" + name, {})
        if package.get("version") != version or not package.get("integrity", "").startswith(
            "sha512-"
        ):
            raise ValueError(f"npm 依赖未精确锁定或缺少完整性信息：{name}")

    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-zA-Z0-9.+-]*", version):
        raise ValueError("无效的发行版本号")
    with tempfile.TemporaryDirectory(prefix="repo-agent-dist-") as temporary:
        work = Path(temporary)
        stage = work / "source"
        stage.mkdir()
        copy_files(root, stage, sources)
        bundle = work / "bundle"
        bundle.mkdir()
        environment = work / "build-env"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = str(environment_python(environment))
        package_flags = (["--no-index", "--no-cache-dir"] if offline else [])
        if wheelhouse:
            package_flags += ["--find-links", str(Path(wheelhouse).resolve(strict=True))]
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--require-hashes",
                *package_flags,
                "--only-binary=:all:",
                "-r",
                str(stage / "requirements-build.lock"),
            ],
            check=True,
        )
        subprocess.run(
            [
                python,
                "-m",
                "build",
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(work / "wheels"),
                str(stage),
            ],
            check=True,
        )
        (wheel,) = (work / "wheels").glob("*.whl")
        verify_wheel(wheel, stage)
        (bundle / "wheels").mkdir()
        shutil.copy2(wheel, bundle / "wheels" / wheel.name)
        with zipfile.ZipFile(wheel) as archive:
            for name in [*(destination for _, destination in RESOURCE_FILES), CONTEXT_ARCHIVE]:
                resource_target = bundle / name
                resource_target.parent.mkdir(parents=True, exist_ok=True)
                resource_target.write_bytes(archive.read(name))
        results = []
        for selected in targets:
            print(f"准备平台完整包：{selected}", flush=True)
            kit = work / f'python-kit-{selected}'
            prepare(root, kit, selected, archive=archives[selected],
                    wheelhouse=wheelhouse, offline=offline)
            platform_bundle = work / f'bundle-{selected}'
            shutil.copytree(bundle, platform_bundle)
            copy_files(root, platform_bundle, bootstrap_files(root, target=selected))
            shutil.copytree(kit, platform_bundle, dirs_exist_ok=True)
            results.append(write_release(platform_bundle, output, version, selected, wheel.name))
        return results


def main():
    parser = argparse.ArgumentParser(
        description="构建平台完整发行包；默认构建所有支持的平台，包含 Python 和运行依赖"
    )
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "dist",
                        help="输出根目录，产物放入 <目录>/<版本>/<平台>/")
    parser.add_argument("--uv", default=os.environ.get("UV") or shutil.which("uv"))
    parser.add_argument('--target', choices=tuple(runtime_records()),
                        help='仅构建指定平台；省略则构建 runtime/python.lock 中的全部平台')
    parser.add_argument('--runtime-archive', type=Path,
                        help='单平台 Python 归档，或包含 <平台>.tar.gz 的目录')
    parser.add_argument('--wheelhouse', type=Path,
                        help='本地 wheel 目录；全平台离线构建需包含所有目标及构建依赖')
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if not args.uv:
        parser.error("构建需要 uv；使用 --uv 指定路径，普通用户安装不需要 uv")
    try:
        build(Path(__file__).resolve().parents[1], args.output.expanduser().resolve(), args.uv,
              target=args.target, runtime_archive=args.runtime_archive,
              wheelhouse=args.wheelhouse, offline=args.offline)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"构建未完成：{error}\n")


if __name__ == "__main__":
    main()
