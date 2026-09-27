"""Host-service contracts and packaging boundaries, independent of sandbox hardware."""

import errno
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import build_manifest
from host_support import filesystem, storage
from host_support.archives import archive_path
from host_support.execution import backend_capabilities
from host_support.languages import sandbox_environment, tool_search_path
from host_support.locking import lock_descriptor
from host_support.paths import app_directory, environment_python, installed_command
from host_support.platforms import PlatformInfo, release_target

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_payload_imports_without_site_packages_or_business_modules(tmp_path):
    build_manifest.copy_files(ROOT, tmp_path, build_manifest.bootstrap_files(ROOT))
    code = """
import importlib, json, pkgutil, sys
sys.path.insert(0, sys.argv[1])
import host_support
for module in pkgutil.iter_modules(host_support.__path__):
    importlib.import_module('host_support.' + module.name)
assert not any(name.split('.')[0] in {'agent', 'tools', 'sandbox', 'cli', 'packaging'}
               for name in sys.modules)
import cli.setup, cli.doctor, cli.uninstall, cli.release_install
assert not any(name.split('.')[0] in {'agent', 'tools', 'sandbox', 'httpx', 'yaml'}
               for name in sys.modules)
print('stdlib-only')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib-only"


@pytest.mark.parametrize(
    "system,machine,target",
    [
        ("Darwin", "arm64", "macos-arm64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "AMD64", "linux-x86_64"),
        ("Windows", "AMD64", "windows-x86_64"),
    ],
)
def test_identity_is_normalized_without_enabling_unsupported_releases(system, machine, target):
    assert PlatformInfo.from_names(system, machine).target == target
    if system == "Windows":
        assert release_target(target).runtime_python == "python/python.exe"
        assert release_target(target).wheel_platforms() == ["win_amd64"]
    else:
        assert release_target(target).runtime_python == "python/bin/python3"


def test_host_and_execution_guest_capabilities_are_independent():
    assert not backend_capabilities("native", platform="win32").supported
    assert backend_capabilities("docker", platform="win32").writeback
    assert not backend_capabilities("native", platform="darwin").gpu
    assert backend_capabilities("native", platform="linux").gpu
    assert not backend_capabilities("local", platform="linux").project_python


def test_posix_layouts_and_overrides_remain_consistent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "private state"))
    assert app_directory("state") == tmp_path / "private state/repo-agent"
    from cli.installation import registry_dir
    from tools._internal.file_policy import session_state_root

    assert registry_dir().parent == session_state_root().parent == app_directory("state")
    assert installed_command(tmp_path, "repo-agent") == tmp_path / ".venv/bin/repo-agent"
    assert environment_python(tmp_path, platform="darwin") == tmp_path / "bin/python"
    assert environment_python(tmp_path, platform="linux", kind="conda") == tmp_path / "bin/python"
    assert environment_python(tmp_path, platform="win32") == tmp_path / "Scripts/python.exe"
    assert environment_python(tmp_path, platform="win32", kind="conda") == tmp_path / "python.exe"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_probe_and_sandbox_paths_agree_without_inheriting_credentials(
    tmp_path, monkeypatch, platform
):
    python, scratch = tmp_path / ".venv/bin/python", tmp_path / "scratch"
    monkeypatch.setenv("PATH", "/untrusted/project")
    monkeypatch.setenv("LLM_API_KEY", "must-not-reach-worker")
    monkeypatch.setenv("HTTPS_PROXY", "must-not-reach-worker")
    env = sandbox_environment(python, scratch, platform=platform)
    assert env["PATH"] == tool_search_path(python, platform=platform)
    assert not {"LLM_API_KEY", "HTTPS_PROXY"} & env.keys()
    assert "/untrusted/project" not in env["PATH"]
    assert env["GOPROXY"] == "off" and env["HOME"] == str(scratch)
    assert ("/opt/homebrew/bin" in env["PATH"]) == (platform == "darwin")


@pytest.mark.parametrize("failure", ["sync", "conflict", "replace"])
def test_atomic_write_failure_preserves_original_and_cleans_staging(tmp_path, monkeypatch, failure):
    target = tmp_path / "state.json"
    target.write_bytes(b"original")

    def fail(*args):
        raise OSError("injected failure")

    if failure == "sync":
        monkeypatch.setattr(storage.os, "fsync", fail)
    elif failure == "replace":
        monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError, match="injected failure"):
        storage.atomic_write(
            target, b"new", sync=True, before_replace=fail if failure == "conflict" else None
        )
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]


def test_descriptor_lock_is_exclusive_and_released_by_close(tmp_path):
    path = tmp_path / "lock"
    first = filesystem.open_file(path, os.O_RDWR | os.O_CREAT)
    second = filesystem.open_file(path, os.O_RDWR)
    try:
        lock_descriptor(first, blocking=False)
        with pytest.raises(BlockingIOError):
            lock_descriptor(second, blocking=False)
        os.close(first)
        first = None
        lock_descriptor(second, blocking=False)
    finally:
        if first is not None:
            os.close(first)
        os.close(second)
    assert path.exists()


def test_unsupported_file_service_never_drops_nofollow_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "os", SimpleNamespace(name="unsupported"))
    with pytest.raises(OSError) as error:
        filesystem.open_file(tmp_path / "file")
    assert error.value.errno == errno.ENOTSUP


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "C:relative",
        "dir\\entry",
        "\\\\server\\share",
        "bad\0name",
    ],
)
def test_archive_paths_have_one_portable_interpretation(name):
    with pytest.raises(ValueError):
        archive_path(name)


def test_native_platforms_share_lifecycle_without_inheriting_macos_policy(monkeypatch, tmp_path):
    from sandbox import native
    from sandbox.linux_native import LinuxNativeBackend
    from sandbox.macos_native import MacOSNativeBackend
    from sandbox.native_common import NativeBackendBase

    assert issubclass(LinuxNativeBackend, NativeBackendBase)
    assert not issubclass(LinuxNativeBackend, MacOSNativeBackend)
    monkeypatch.setattr(LinuxNativeBackend, "__init__", lambda *a, **kw: None)
    monkeypatch.setattr(MacOSNativeBackend, "__init__", lambda *a, **kw: None)
    for platform, expected in (("linux", LinuxNativeBackend), ("darwin", MacOSNativeBackend)):
        monkeypatch.setattr(native, "sys", SimpleNamespace(platform=platform))
        assert type(native.create_native_backend(tmp_path)) is expected
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="linux"))
    assert type(native.NativeBackend.__new__(native.NativeBackend)) is LinuxNativeBackend
    assert type(native.NativeBackend(tmp_path)) is LinuxNativeBackend
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="不会退回"):
        native.create_native_backend(tmp_path)
