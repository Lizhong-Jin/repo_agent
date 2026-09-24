"""Build complete wheels and source distributions from an explicit resource allowlist."""

import shutil
import tarfile
from pathlib import Path

from setuptools.command.build_py import build_py

PACKAGES = ("agent", "cli", "llm", "sandbox", "tools")
ROOT_FILES = (
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


def source_files(root):
    for name in ROOT_FILES:
        path = root / name
        if path.is_file():
            yield path
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and (path.suffix == ".py" or path.name == "SKILL.md" or path.name == "Dockerfile")
            ):
                yield path
    for path in sorted((root / "dependencies").rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.name in {"package.json", "package-lock.json"}
        ):
            yield path


class BuildPy(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        assets = Path(self.build_lib) / "cli/resources"
        assets.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / ".env.example", assets / "default.env")
        for name in ("requirements-core.lock", "requirements-lsp.lock", "requirements-build.lock"):
            shutil.copyfile(root / name, assets / name)
        node = assets / "dependencies/node"
        node.mkdir(parents=True, exist_ok=True)
        for name in ("package.json", "package-lock.json"):
            shutil.copyfile(root / "dependencies/node" / name, node / name)
        with tarfile.open(assets / "docker-context.tar.gz", "w:gz") as bundle:
            for path in source_files(root):
                bundle.add(path, arcname=str(path.relative_to(root)), recursive=False)
