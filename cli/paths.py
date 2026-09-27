"""Locate installation state and shipped resources without consulting the task directory."""

import json
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()

from host_support.archives import archive_path


def code_root():
    return Path(__file__).resolve().parents[1]


def installation_root():
    source = code_root()
    prefix = Path(sys.prefix).resolve()
    if prefix.name == ".venv" and source.is_relative_to(prefix):
        return prefix.parent
    return source


def resource_path(name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("无效的资源名称")
    packaged = Path(__file__).resolve().parent / "resources" / relative
    if packaged.is_file():
        return packaged
    source = code_root() / (".env.example" if name == "default.env" else name)
    if source.is_file():
        return source
    raise ValueError(f"安装资源缺失：{name}；请重新安装完整发行包")


def extract_files(archive, destination):
    """Accept only ordinary relative files/directories, never tar links or devices."""
    destination = Path(destination).resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        seen = set()
        if sum(item.size for item in members) > 512 * 1024 * 1024:
            raise ValueError("发行包解压大小超过限制")
        for item in members:
            name = archive_path(item.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or str(name) in seen
                or not (item.isfile() or item.isdir())
            ):
                raise ValueError("发行包含不安全或重复的路径")
            seen.add(str(name))
            target = destination / name
            if not target.resolve().is_relative_to(destination):
                raise ValueError("发行包路径越界")
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(item) as source, target.open("xb") as output:
                    import shutil

                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if item.mode & 0o111 else 0o644)


@contextmanager
def docker_build_context():
    source = code_root()
    if (source / "pyproject.toml").is_file() and (source / "sandbox/Dockerfile").is_file():
        # A stale Docker allowlist could otherwise silently omit a newly added module.
        # Load the stdlib policy from this trusted checkout; wheels use their archive below.
        from runpy import run_path

        policy = run_path(str(source / "build_manifest.py"))
        policy["check_configuration"](source)
        policy["source_files"](source)
        yield source
    else:
        with tempfile.TemporaryDirectory(prefix="repo-agent-build-") as temporary:
            root = Path(temporary)
            extract_files(resource_path("docker-context.tar.gz"), root)
            yield root


def version_info():
    root = installation_root()
    try:
        record = json.loads((root / ".repo-agent-install.json").read_text())
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    try:
        from importlib.metadata import PackageNotFoundError, version

        value = version("repo-agent")
    except PackageNotFoundError:
        value = "development"
    return {
        "version": record.get("app_version", value),
        "kind": record.get("kind", "development"),
        "installation": str(root),
        "python": sys.executable,
    }
