"""Single distribution policy; stdlib only so bootstrapping needs no build backend.

Edit declarations here, then run ``python3 build_manifest.py --write``.
Generated configuration is checked by both wheel and release builds.
"""

import argparse
import io
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

PACKAGES = ("agent", "cli", "llm", "sandbox", "tools")
LOCK_FILES = tuple(f"requirements-{kind}.lock" for kind in ("core", "lsp", "build", "dev"))
# Files installed in cli/resources, with explicit source -> wheel destination mapping.
RESOURCE_FILES = (
    (".env.example", "cli/resources/default.env"),
    *((name, "cli/resources/" + name) for name in LOCK_FILES),
    *(
        ("dependencies/node/" + name, "cli/resources/dependencies/node/" + name)
        for name in ("package.json", "package-lock.json")
    ),
)
# Non-Python files that keep their package-relative path in wheels and build contexts.
PACKAGE_RESOURCES = ("agent/skills/builtin/*/SKILL.md", "sandbox/Dockerfile")
PUBLIC_METADATA = ("pyproject.toml", "uv.lock")
BUILD_FILES = ("build_manifest.py", "build_support.py", "MANIFEST.in", ".dockerignore")
INSTALL_SCRIPTS = ("install.sh", "install-release.sh", "uninstall.sh")
CONTEXT_ARCHIVE = "cli/resources/docker-context.tar.gz"
EXCLUDED_DIRS = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "logs",
    ".pytest_cache",
    ".ruff_cache",
)
BEGIN = "# BEGIN generated distribution settings (build_manifest.py)"
END = "# END generated distribution settings"


def regular_file(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Invalid distribution path: {name}")
    path = root / relative
    if any(
        parent.is_symlink()
        for parent in [path, *path.parents]
        if parent != root and parent.is_relative_to(root)
    ):
        raise ValueError(f"Distribution file cannot be a symlink: {name}")
    if not path.is_file():
        raise ValueError(f"Required distribution file is missing: {name}")
    return path


def package_sources(root):
    result = set()
    for package in PACKAGES:
        regular_file(root, package + "/__init__.py")
        for directory, dirs, files in os.walk(root / package, followlinks=False):
            dirs[:] = sorted(
                name for name in dirs if name not in EXCLUDED_DIRS and not name.startswith(".")
            )
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise ValueError(
                        f"Distribution package directory cannot be a symlink: {directory}/{name}"
                    )
            for name in files:
                if name.endswith(".py") and not name.startswith("."):
                    relative = (Path(directory) / name).relative_to(root).as_posix()
                    regular_file(root, relative)
                    result.add(relative)
    return sorted(result)


def package_resources(root):
    result = set()
    for pattern in PACKAGE_RESOURCES:
        matches = sorted(root.glob(pattern))
        if not matches:
            raise ValueError(f"Required distribution resource is missing: {pattern}")
        for path in matches:
            relative = path.relative_to(root).as_posix()
            regular_file(root, relative)
            if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
                raise ValueError(f"Resource uses an excluded directory: {relative}")
            result.add(relative)
    return sorted(result)


def source_names(root):
    """Enumerate inputs independently of generated configuration file existence."""
    names = {
        *PUBLIC_METADATA,
        *BUILD_FILES,
        *(src for src, _ in RESOURCE_FILES),
        *package_sources(root),
        *package_resources(root),
    }
    return sorted(names)


def source_files(root):
    """Complete validated wheel/Docker inputs (not installer scripts)."""
    return [regular_file(root, name) for name in source_names(root)]


def bootstrap_files(root):
    """Installer/recovery payload, deliberately separate from wheel build inputs."""
    names = {
        *INSTALL_SCRIPTS,
        *PUBLIC_METADATA,
        ".env.example",
        *LOCK_FILES,
        *(name for name in package_sources(root) if name.startswith("cli/")),
    }
    return [regular_file(root, name) for name in sorted(names)]


def copy_files(root, destination, files):
    for source in files:
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def setuptools_settings():
    includes = [rule for package in PACKAGES for rule in (package, package + ".*")]
    excludes = [rule for name in EXCLUDED_DIRS for rule in ("*." + name, "*." + name + ".*")]
    data = {}
    for pattern in [*PACKAGE_RESOURCES, *(dst for _, dst in RESOURCE_FILES), CONTEXT_ARCHIVE]:
        package, relative = pattern.split("/", 1)
        data.setdefault(package, []).append(relative)
    lines = [
        BEGIN,
        "[tool.setuptools]",
        "include-package-data = false",
        "",
        "[tool.setuptools.packages.find]",
        "include = " + json.dumps(includes),
        "exclude = " + json.dumps(excludes),
        "",
        "[tool.setuptools.package-data]",
    ]
    lines.extend(
        f"{json.dumps(package)} = {json.dumps(sorted(set(patterns)))}"
        for package, patterns in sorted(data.items())
    )
    lines += [
        "",
        "[tool.setuptools.cmdclass]",
        'build_py = "build_support.BuildPy"',
        'sdist = "build_support.Sdist"',
    ]
    return "\n".join([*lines, END])


def generated_configs(root):
    required = sorted({*PUBLIC_METADATA, *BUILD_FILES, *(src for src, _ in RESOURCE_FILES)})
    manifest = [
        "# Generated by build_manifest.py --write; do not edit.",
        *("include " + name for name in required),
        *("recursive-include " + package + " *.py" for package in PACKAGES),
        *("include " + pattern for pattern in PACKAGE_RESOURCES),
    ]
    for package in PACKAGES:
        for directory in EXCLUDED_DIRS:
            manifest += [f"prune {package}/{directory}", f"prune {package}/**/{directory}"]
    manifest += ["global-exclude *.pyc *.pyo .env .env.*", "include .env.example"]
    # Docker patterns do not distinguish directories by a trailing slash. Use exact
    # selected file paths, reopening parents but immediately excluding their children.
    # This prevents an allowed directory from implicitly admitting unlisted JSON/secrets.
    names = source_names(root)
    directories = {
        parent.as_posix() for name in names for parent in Path(name).parents if parent != Path(".")
    }
    docker = ["# Generated by build_manifest.py --write; do not edit.", "**"]
    for directory in sorted(directories, key=lambda name: (name.count("/"), name)):
        docker += ["!" + directory, directory + "/**"]
    docker += ["!" + name for name in names]
    return {"MANIFEST.in": "\n".join(manifest) + "\n", ".dockerignore": "\n".join(docker) + "\n"}


def check_configuration(root, *, write=False):
    for name, expected in generated_configs(root).items():
        if write:
            (root / name).write_text(expected, encoding="utf-8")
        elif not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != expected:
            raise ValueError(f"{name} is out of sync; run python3 build_manifest.py --write")
    path = root / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise ValueError("pyproject.toml is missing distribution settings markers")
    start, end = text.index(BEGIN), text.index(END) + len(END)
    expected = setuptools_settings()
    if write:
        path.write_text(text[:start] + expected + text[end:], encoding="utf-8")
    elif text[start:end] != expected:
        raise ValueError(
            "pyproject.toml distribution settings are out of sync; run python3 build_manifest.py --write"
        )


def _verify_tar(data, expected):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if (
            len(names) != len(set(names))
            or set(names) != set(expected)
            or any(not item.isfile() for item in members)
        ):
            raise ValueError(
                "Build context/archive contains missing, duplicate or unexpected files"
            )
        for item in members:
            if archive.extractfile(item).read() != expected[item.name].read_bytes():
                raise ValueError(f"Archive content differs from source: {item.name}")


def verify_wheel(wheel, root):
    expected = {name: root / name for name in [*package_sources(root), *package_resources(root)]}
    for source, destination in RESOURCE_FILES:
        if destination in expected:
            raise ValueError(f"Duplicate wheel resource destination: {destination}")
        expected[destination] = regular_file(root, source)
    context = {path.relative_to(root).as_posix(): path for path in source_files(root)}
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        payload = {
            name
            for name in names
            if not name.endswith("/") and not name.split("/", 1)[0].endswith(".dist-info")
        }
        if len(names) != len(set(names)) or payload != {*expected, CONTEXT_ARCHIVE}:
            raise ValueError(
                f"Wheel file list mismatch; missing={sorted(({*expected, CONTEXT_ARCHIVE}) - payload)}, unexpected={sorted(payload - {*expected, CONTEXT_ARCHIVE})}"
            )
        for name, source in expected.items():
            if archive.read(name) != source.read_bytes():
                raise ValueError(f"Wheel content differs from source: {name}")
        _verify_tar(archive.read(CONTEXT_ARCHIVE), context)


def verify_release_archive(archive, bundle, *, prefix=None):
    expected = {
        (
            (Path(prefix) / path.relative_to(bundle)).as_posix()
            if prefix
            else path.relative_to(bundle).as_posix()
        ): path
        for path in bundle.rglob("*")
        if path.is_file()
    }
    _verify_tar(archive.read_bytes(), expected)


def main():
    parser = argparse.ArgumentParser(description="检查或生成统一分发清单的配套配置")
    parser.add_argument(
        "--write",
        action="store_true",
        help="更新 MANIFEST.in、.dockerignore 和 pyproject 的生成区块",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    check_configuration(root, write=args.write)
    source_files(root)
    bootstrap_files(root)
    print("分发清单与打包配置一致，必需文件完整。")


if __name__ == "__main__":
    main()
