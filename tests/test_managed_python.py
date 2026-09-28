"""Bootstrap without system Python; offline failures preserve existing installs."""

import hashlib
import io
import os
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from installer.install_packages import requirement_args, requirement_files, source_flags

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def kit(tmp_path):
    root = tmp_path / "source with spaces"
    (root / "runtime").mkdir(parents=True)
    script = root / "bootstrap.sh"
    shutil.copy2(ROOT / "scripts/bootstrap-python.sh", script)
    archive = root / "runtime/python.tar.gz"
    data = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo("python/bin/python3")
        item.mode, item.size = 0o755, len(data)
        tar.addfile(item, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    system = "macos" if platform.system() == "Darwin" else "linux"
    arch = "arm64" if platform.machine() in {"arm64", "aarch64"} else "x86_64"
    (root / "runtime/python.lock").write_text(
        f"{system}-{arch} 3.13.15 {digest} https://invalid.example/python.tar.gz\n"
    )
    return root, dict(os.environ, AGENT_PYTHON_CACHE=str(tmp_path / "cache"))


def bootstrap(kit, *args):
    root, env = kit
    return subprocess.run(
        ["/bin/bash", str(root / "bootstrap.sh"), str(root), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_offline_runtime_install_and_reuse_after_source_removed(kit):
    result = bootstrap(kit, "--offline")
    assert result.returncode == 0, result.stderr
    python = Path(result.stdout.strip())
    assert python.is_file()
    (kit[0] / "runtime/python.tar.gz").unlink()
    again = bootstrap(kit, "--offline")
    assert again.returncode == 0 and again.stdout == result.stdout


def test_bad_runtime_hash_never_executes_or_publishes(kit):
    (kit[0] / "runtime/python.tar.gz").write_bytes(b"corrupt")
    result = bootstrap(kit, "--offline")
    assert result.returncode != 0 and "SHA256" in result.stderr
    assert not list(Path(kit[1]["AGENT_PYTHON_CACHE"]).glob("*/.verified"))


def test_offline_missing_runtime_does_not_download(kit):
    (kit[0] / "runtime/python.tar.gz").unlink()
    result = bootstrap(kit, "--offline")
    assert result.returncode != 0 and "离线安装缺少 Python" in result.stderr


def test_readonly_bootstrap_does_not_create_cache(kit):
    result = bootstrap(kit, "--check")
    assert result.returncode != 0 and "尚无已安装" in result.stderr
    assert not Path(kit[1]["AGENT_PYTHON_CACHE"]).exists()


def test_wrong_platform_bundle_rejected_before_using_cache(kit):
    (kit[0] / "runtime/target").write_text("other-platform")
    assert "平台不匹配" in bootstrap(kit).stderr


def test_source_offline_dependency_selection_includes_build_and_dev(tmp_path):
    files = requirement_files(tmp_path, native=True, source=True)
    assert [p.name for p in files] == [
        "requirements-lsp.lock",
        "requirements-build.lock",
        "requirements-dev.lock",
    ]
    assert requirement_args(files)[::2] == ["-r"] * 3
    assert "--no-index" in source_flags(tmp_path, offline=True)
    with pytest.raises(ValueError, match="wheelhouse"):
        source_flags(offline=True)
    assert len(requirement_files(tmp_path, native=False, source=False)) == 1
