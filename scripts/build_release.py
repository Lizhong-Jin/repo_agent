"""Build a source-independent, hash-verified release archive without publishing it."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

from lock_dependencies import export_locks

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


def build(root, output, uv):
    check_configuration(root)
    sources = source_files(root)
    bootstrap = bootstrap_files(root)
    export_locks(root, uv, check=True)
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
    with tempfile.TemporaryDirectory(prefix="repo-agent-dist-") as temporary:
        work = Path(temporary)
        stage = work / "source"
        stage.mkdir()
        copy_files(root, stage, sources)
        bundle = work / "bundle"
        bundle.mkdir()
        copy_files(root, bundle, bootstrap)
        environment = work / "build-env"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = str(environment / "bin/python")
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--require-hashes",
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
            for name in [*(target for _, target in RESOURCE_FILES), CONTEXT_ARCHIVE]:
                target = bundle / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        files = {
            str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(bundle.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema": 1,
            "name": "repo-agent",
            "version": version,
            "wheel": "wheels/" + wheel.name,
            "files": files,
        }
        (bundle / "release.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        )
        output.mkdir(parents=True, exist_ok=True)
        release_directory = f"repo-agent-{version}"
        target = output / f"{release_directory}.tar.gz"
        fd, temporary_archive = tempfile.mkstemp(prefix=".release-", dir=output)
        os.close(fd)
        try:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_file():
                        archive.add(
                            path,
                            arcname=f"{release_directory}/{path.relative_to(bundle).as_posix()}",
                            recursive=False,
                        )
            verify_release_archive(Path(temporary_archive), bundle, prefix=release_directory)
            os.replace(temporary_archive, target)
        finally:
            Path(temporary_archive).unlink(missing_ok=True)
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_name(target.name + ".sha256").write_text(f"{checksum}  {target.name}\n")
        shutil.copy2(wheel, output / wheel.name)
        print(f"发行包：{target}\nSHA256：{checksum}")
        return target


def main():
    parser = argparse.ArgumentParser(
        description="构建独立发行版，包含 wheel、安装器、资源和固定依赖清单"
    )
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "dist")
    parser.add_argument("--uv", default=os.environ.get("UV") or shutil.which("uv"))
    args = parser.parse_args()
    if not args.uv:
        parser.error("构建需要 uv；使用 --uv 指定路径，普通用户安装不需要 uv")
    build(Path(__file__).resolve().parents[1], args.output.expanduser().resolve(), args.uv)


if __name__ == "__main__":
    main()
