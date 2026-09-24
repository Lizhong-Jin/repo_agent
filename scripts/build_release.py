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


def build(root, output, uv):
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
        # Only tracked-purpose sources/resources; never package a venv, user config, or logs.
        top = (
            "pyproject.toml",
            "build_support.py",
            "MANIFEST.in",
            ".env.example",
            ".dockerignore",
            "uv.lock",
            "requirements-core.lock",
            "requirements-lsp.lock",
            "requirements-build.lock",
        )
        for name in top:
            shutil.copy2(root / name, stage / name)
        for package in ("agent", "cli", "llm", "sandbox", "tools"):
            for path in (root / package).rglob("*"):
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and (path.suffix == ".py" or path.name in {"SKILL.md", "Dockerfile"})
                ):
                    target = stage / path.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
        for name in ("package.json", "package-lock.json"):
            target = stage / "dependencies/node" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / "dependencies/node" / name, target)
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
        bundle = work / "bundle"
        (bundle / "wheels").mkdir(parents=True)
        shutil.copy2(wheel, bundle / "wheels" / wheel.name)
        for name in (
            "install.sh",
            "install-release.sh",
            "uninstall.sh",
            ".env.example",
            "pyproject.toml",
            "uv.lock",
            "requirements-core.lock",
            "requirements-lsp.lock",
            "requirements-build.lock",
        ):
            shutil.copy2(root / name, bundle / name)
        # Bootstrap and recovery remain usable even if the installed venv is unavailable.
        (bundle / "cli").mkdir()
        for path in (stage / "cli").glob("*.py"):
            shutil.copy2(path, bundle / "cli" / path.name)
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.startswith("cli/resources/") and not name.endswith("/"):
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
        target = output / f"repo-agent-{version}.tar.gz"
        fd, temporary_archive = tempfile.mkstemp(prefix=".release-", dir=output)
        os.close(fd)
        try:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_file():
                        archive.add(path, arcname=str(path.relative_to(bundle)), recursive=False)
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
