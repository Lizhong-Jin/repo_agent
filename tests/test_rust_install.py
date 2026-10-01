"""Optional compilation, compiler-free binary installs, and release target selection."""

import subprocess
from pathlib import Path

import pytest

from host_support.platforms import PlatformInfo
from installer import rust_extension
from installer.release_manifest import digest
from scripts import rust_wheels


@pytest.fixture
def linux_host(monkeypatch):
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("linux", "x86_64"))


def test_missing_compiler_keeps_core_install(tmp_path, monkeypatch, linux_host, capsys):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "Cargo.toml").touch()
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    marker = tmp_path / "core-installed"
    marker.write_text("keep")
    monkeypatch.setattr(rust_extension.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        rust_extension, "run_download", lambda *a, **kw: pytest.fail("No compiler available")
    )
    assert not rust_extension.install_rust_extension(tmp_path)
    assert marker.read_text() == "keep"
    assert "cargo/rustc" in capsys.readouterr().out


@pytest.mark.parametrize(
    "error",
    [
        ValueError("build failed"),
        OSError("linker missing"),
        KeyboardInterrupt(),
        subprocess.TimeoutExpired("build", 10),
        subprocess.CalledProcessError(1, "build"),
    ],
)
def test_build_failure_is_optional(tmp_path, monkeypatch, linux_host, error, capsys):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(rust_extension, "build_rust_wheel", fail)
    assert not rust_extension.install_rust_extension(tmp_path)
    assert "核心安装" in capsys.readouterr().out


def test_source_build_uses_external_directory_and_respects_offline(tmp_path, monkeypatch):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "Cargo.toml").touch()
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    output = tmp_path / "wheels"
    calls = []
    monkeypatch.setattr(rust_extension.shutil, "which", lambda name: "/bin/" + name)

    def build(command, **kwargs):
        calls.append((command, kwargs))
        staged = Path(command[command.index("--wheel-dir") + 1])
        wheel = staged / "rust_backend-0.1.0-cp311-abi3-linux_x86_64.whl"
        wheel.write_bytes(b"built")

    monkeypatch.setattr(rust_extension, "run_download", build)
    result = rust_extension.build_rust_wheel(
        tmp_path,
        "/agent/python",
        output,
        offline=True,
        wheelhouse=tmp_path,
        compatibility="manylinux_2_28",
    )
    assert result.read_bytes() == b"built"
    command, kwargs = calls[0]
    assert command[:5] == ["/agent/python", "-B", "-m", "pip", "wheel"]
    assert "--no-index" in command and "--no-deps" in command
    assert kwargs["env"]["CARGO_NET_OFFLINE"] == "true"
    assert kwargs["env"]["MATURIN_PEP517_ARGS"] == "--locked --compatibility manylinux_2_28"
    target = Path(kwargs["env"]["CARGO_TARGET_DIR"])
    assert not target.is_relative_to(tmp_path)
    assert not target.parent.exists()


def release_with_wheel(root):
    wheel = root / "wheels/rust_backend-0.1.0-cp311-abi3-manylinux_2_28_x86_64.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"precompiled")
    name = wheel.relative_to(root).as_posix()
    return wheel, {"rust_wheel": name, "files": {name: digest(wheel)}}


def test_release_installs_offline_without_rust_compiler(tmp_path, monkeypatch, linux_host):
    wheel, release = release_with_wheel(tmp_path)
    calls = []
    monkeypatch.setattr(rust_extension.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        rust_extension, "build_rust_wheel", lambda *a, **kw: pytest.fail("Release cannot compile")
    )
    monkeypatch.setattr(rust_extension, "run_download", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(rust_extension.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    assert rust_extension.install_rust_extension(tmp_path, release=release, offline=True)
    assert "--no-index" in calls[0] and "--no-deps" in calls[0]
    assert str(wheel) in calls[0]
    assert calls[0][0] == str(tmp_path / ".venv/bin/python")
    assert "rust_backend.API_VERSION == 1" in calls[1][-1]


def test_corrupt_release_wheel_is_not_installed(tmp_path, monkeypatch, linux_host):
    wheel, release = release_with_wheel(tmp_path)
    wheel.write_bytes(b"tampered")
    monkeypatch.setattr(
        rust_extension, "run_download", lambda *a, **kw: pytest.fail("Corrupt wheel installed")
    )
    assert not rust_extension.install_rust_extension(tmp_path, release=release)


def test_unloadable_binary_is_not_reported_ready(tmp_path, monkeypatch, linux_host, capsys):
    _, release = release_with_wheel(tmp_path)
    monkeypatch.setattr(rust_extension, "run_download", lambda *a, **kw: None)

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["python"], stderr="private-content")

    monkeypatch.setattr(rust_extension.subprocess, "run", fail)
    assert not rust_extension.install_rust_extension(tmp_path, release=release)
    output = capsys.readouterr().out
    assert "[OK]" not in output and "private-content" not in output


def test_legacy_release_does_not_attempt_compilation(tmp_path, monkeypatch, linux_host):
    monkeypatch.setattr(
        rust_extension, "build_rust_wheel", lambda *a, **kw: pytest.fail("Legacy release compiled")
    )
    assert not rust_extension.install_rust_extension(tmp_path, release={"files": {}})


def test_windows_skips_unsupported_scanner(tmp_path, monkeypatch):
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("windows", "x86_64"))
    monkeypatch.setattr(
        rust_extension, "build_rust_wheel", lambda *a, **kw: pytest.fail("Windows compiled")
    )
    assert not rust_extension.install_rust_extension(tmp_path)


@pytest.mark.parametrize(
    "target,tag",
    [
        ("linux-x86_64", "manylinux_2_28_x86_64"),
        ("linux-arm64", "manylinux_2_28_aarch64"),
        ("macos-arm64", "macosx_11_0_arm64"),
        ("macos-x86_64", "macosx_10_15_x86_64"),
    ],
)
def test_release_selects_matching_platform_and_abi(tmp_path, target, tag):
    name = "rust_backend-0.1.0-cp311-abi3-" + tag + ".whl"
    wheel = tmp_path / name
    wheel.touch()
    assert rust_wheels.select_rust_wheel(tmp_path, target, "3.13.15", "0.1.0") == wheel
    assert rust_wheels.select_rust_wheel(tmp_path, target, "3.10.1", "0.1.0") is None
    assert rust_wheels.select_rust_wheel(tmp_path, target, "3.13.15", "0.2.0") is None


@pytest.mark.parametrize(
    "tag",
    [
        "cp311-abi3-manylinux_2_28_aarch64",
        "cp311-abi3-macosx_11_0_arm64",
        "cp311-abi3-manylinux_2_35_x86_64",
        "cp313-cp313-manylinux_2_28_x86_64",
        "cp311-abi3-linux_x86_64",
        "py3-none-any",
    ],
)
def test_release_rejects_wrong_platform_or_compatibility_floor(tmp_path, tag):
    (tmp_path / ("rust_backend-0.1.0-" + tag + ".whl")).touch()
    assert rust_wheels.select_rust_wheel(tmp_path, "linux-x86_64", "3.13.15", "0.1.0") is None


def test_cross_platform_release_uses_prebuilt_and_never_cross_compiles(tmp_path, monkeypatch):
    root = tmp_path / "source"
    source = root / "rust"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("macos", "arm64"))
    monkeypatch.setattr(
        rust_wheels, "build_rust_wheel", lambda *a, **kw: pytest.fail("Cross compilation attempted")
    )
    records = {"linux-x86_64": {"version": "3.13.15"}, "windows-x86_64": {"version": "3.13.15"}}
    assert rust_wheels.prepare_rust_wheels(root, records, tmp_path / "empty") == {}
    with pytest.raises(ValueError, match="linux-x86_64"):
        rust_wheels.prepare_rust_wheels(root, records, tmp_path / "required", required=True)
    wheel = tmp_path / "rust_backend-0.1.0-cp311-abi3-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"prebuilt")
    selected = rust_wheels.prepare_rust_wheels(
        root, records, tmp_path / "prepared", wheelhouse=tmp_path, required=True, offline=True
    )
    assert set(selected) == {"linux-x86_64"}
    assert selected["linux-x86_64"].read_bytes() == b"prebuilt"


def test_local_release_build_requires_portable_linux_wheel(tmp_path, monkeypatch, linux_host):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')

    def build(root, python, output, **kwargs):
        assert kwargs["compatibility"] == "manylinux_2_28"
        output.mkdir()
        wheel = output / "rust_backend-0.1.0-cp311-abi3-manylinux_2_28_x86_64.whl"
        wheel.write_bytes(b"compiled")
        return wheel

    monkeypatch.setattr(rust_wheels, "build_rust_wheel", build)
    result = rust_wheels.prepare_rust_wheels(
        tmp_path, {"linux-x86_64": {"version": "3.13.15"}}, tmp_path / "wheels", required=True
    )
    assert result["linux-x86_64"].read_bytes() == b"compiled"


@pytest.mark.parametrize("produced", [0, 1, 2])
def test_build_publishes_only_complete_current_wheel(tmp_path, monkeypatch, produced):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "Cargo.toml").touch()
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    output = tmp_path / "rust_wheels/0.1.0"
    output.mkdir(parents=True)
    name = "rust_backend-0.1.0-cp311-abi3-linux_x86_64.whl"
    old = output / name
    old.write_bytes(b"previous")
    other = output / "rust_backend-0.1.0-cp311-abi3-macosx_11_0_arm64.whl"
    other.write_bytes(b"other-platform")
    monkeypatch.setattr(rust_extension.shutil, "which", lambda name: "/bin/" + name)

    def build(command, **kwargs):
        assert "--no-build-isolation" in command and "--no-index" in command
        staged = Path(command[command.index("--wheel-dir") + 1])
        assert not staged.is_relative_to(tmp_path)
        if produced:
            (staged / name).write_bytes(b"current")
        if produced == 2:
            (staged / other.name).write_bytes(b"ambiguous")

    monkeypatch.setattr(rust_extension, "run_download", build)
    if produced == 1:
        wheel = rust_extension.build_rust_wheel(
            tmp_path, "/agent/python", offline=True, build_isolation=False
        )
        assert wheel == old and wheel.read_bytes() == b"current"
        assert wheel.stat().st_nlink == 1
    else:
        with pytest.raises(ValueError, match="唯一"):
            rust_extension.build_rust_wheel(
                tmp_path, "/agent/python", offline=True, build_isolation=False
            )
        assert old.read_bytes() == b"previous"
    assert other.read_bytes() == b"other-platform"


def test_release_uses_default_artifacts_without_compiler(tmp_path, monkeypatch, linux_host):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    output = tmp_path / "rust_wheels/0.1.0"
    output.mkdir(parents=True)
    name = "rust_backend-0.1.0-cp311-abi3-manylinux_2_28_x86_64.whl"
    (output / name).write_bytes(b"default")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / name).write_bytes(b"explicit")
    monkeypatch.setattr(
        rust_wheels, "build_rust_wheel", lambda *a, **kw: pytest.fail("Unexpected compilation")
    )
    records = {"linux-x86_64": {"version": "3.13.15"}}
    # Using the default artifacts directory as destination must not copy onto itself.
    selected = rust_wheels.prepare_rust_wheels(tmp_path, records, output, required=True)
    assert selected["linux-x86_64"].read_bytes() == b"default"
    selected = rust_wheels.prepare_rust_wheels(
        tmp_path, records, tmp_path / "staging", wheelhouse=explicit, required=True
    )
    assert selected["linux-x86_64"].read_bytes() == b"explicit"


@pytest.mark.parametrize(
    "host,triple,suffix",
    [
        (PlatformInfo("linux", "x86_64"), "x86_64-unknown-linux-gnu", "so"),
        (PlatformInfo("macos", "arm64"), "aarch64-apple-darwin", "dylib"),
    ],
)
def test_compile_library_exports_copy_and_cleans_target(
    tmp_path, monkeypatch, host, triple, suffix
):
    source = tmp_path / "rust"
    source.mkdir(parents=True)
    (source / "Cargo.toml").touch()
    (source / "pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    monkeypatch.setattr(PlatformInfo, "detect", lambda: host)
    monkeypatch.setattr(rust_extension.shutil, "which", lambda name: "/bin/" + name)
    targets = []

    def compile(command, *, env, cwd, check):
        assert "--locked" in command and "--offline" in command
        assert "--release" in command and "extension-module" in command
        assert command[command.index("--target") + 1] == triple
        assert env["PYO3_PYTHON"] == "/agent/python"
        target = Path(env["CARGO_TARGET_DIR"])
        assert not target.is_relative_to(tmp_path)
        targets.append(target)
        binary = target / triple / "release" / f"librust_backend.{suffix}"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"library")
        binary.with_name("hardlinked").hardlink_to(binary)

    monkeypatch.setattr(rust_extension.subprocess, "run", compile)
    artifact = rust_extension.build_rust_library(tmp_path, "/agent/python", offline=True)
    assert artifact == tmp_path / "rust_wheels/0.1.0" / host.target / f"librust_backend.{suffix}"
    assert artifact.read_bytes() == b"library" and artifact.stat().st_nlink == 1
    assert not targets[0].parent.exists()


def test_versioned_wheel_selection_ignores_other_versions_and_old_package(tmp_path):
    for version in ("0.1.0", "0.2.0"):
        directory = tmp_path / version
        directory.mkdir()
        (directory / f"rust_backend-{version}-cp311-abi3-manylinux_2_28_x86_64.whl").touch()
    # An old package with the requested version must not be mistaken for the renamed backend.
    (tmp_path / "0.2.0/repo_agent_policy_scan-0.2.0-cp311-abi3-manylinux_2_28_x86_64.whl").touch()
    wheel = rust_wheels.select_rust_wheel(tmp_path, "linux-x86_64", "3.13.15", "0.2.0")
    assert wheel.parent == tmp_path / "0.2.0"
    assert wheel.name.startswith("rust_backend-0.2.0-")
    assert rust_wheels.select_rust_wheel(tmp_path, "linux-x86_64", "3.13.15", "0.3.0") is None
    assert rust_wheels.select_rust_wheel(wheel.parent, "linux-x86_64", "3.13.15", "0.2.0") == wheel


def test_build_versions_coexist_under_custom_output(tmp_path, monkeypatch):
    source = tmp_path / "rust"
    source.mkdir()
    (source / "Cargo.toml").touch()
    metadata = source / "pyproject.toml"
    monkeypatch.setattr(rust_extension.shutil, "which", lambda name: name)

    def build(command, **kwargs):
        version = rust_extension.rust_version(tmp_path)
        staged = Path(command[command.index("--wheel-dir") + 1])
        (staged / f"rust_backend-{version}-cp311-abi3-macosx_11_0_arm64.whl").write_bytes(
            version.encode()
        )

    monkeypatch.setattr(rust_extension, "run_download", build)
    artifacts = []
    for version in ("0.1.0", "0.2.0"):
        metadata.write_text(f'[project]\nversion="{version}"\n')
        wheel = rust_extension.build_rust_wheel(tmp_path, "python", tmp_path / "custom")
        assert wheel.parent == tmp_path / "custom" / version
        artifacts.append(wheel)
    assert [path.read_bytes() for path in artifacts] == [b"0.1.0", b"0.2.0"]


@pytest.mark.parametrize("version", ["../outside", "/absolute", "", "a/b"])
def test_artifact_version_cannot_escape_output_root(tmp_path, version):
    source = tmp_path / "rust"
    source.mkdir()
    (source / "pyproject.toml").write_text(f'[project]\nversion="{version}"\n')
    with pytest.raises(ValueError, match="版本"):
        rust_extension.artifact_directory(tmp_path)
