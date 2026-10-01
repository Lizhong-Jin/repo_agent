"""Optional Rust scanner builds and binary installation; bootstrap uses only stdlib."""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

from host_support.paths import installed_python
from host_support.platforms import PlatformInfo

from .install_network import run_download
from .install_packages import source_flags
from .release_manifest import digest
from .rust_targets import RUST_TRIPLES

RUST_TARGETS = set(RUST_TRIPLES)


def rust_version(root):
    """Read the extension version once; it must be a safe directory component."""
    version = tomllib.loads((Path(root) / "rust/pyproject.toml").read_text())["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
        raise ValueError("Rust 扩展版本不是有效的目录名")
    return version


def artifact_directory(root, output=None):
    base = Path(output) if output is not None else Path(root) / "rust_wheels"
    return base / rust_version(root)


def _build_source(root):
    source = Path(root) / "rust"
    if not (source / "Cargo.toml").is_file():
        raise ValueError("缺少 Rust 扫描器源码")
    if not shutil.which("cargo") or not shutil.which("rustc"):
        raise ValueError("未找到 cargo/rustc，跳过 Rust 编译")
    if PlatformInfo.detect().target not in RUST_TARGETS:
        raise ValueError("Rust 扫描扩展目前仅支持 Linux/macOS")
    return source.resolve()


def _publish(source, output):
    """Copy, never hardlink, then atomically replace a completed artifact."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=output) as temporary:
        staged = Path(temporary) / source.name
        shutil.copy2(source, staged)
        destination = output / source.name
        os.replace(staged, destination)
    return destination


def build_rust_library(root, python, output=None, *, offline=False, target=None, **wheel_options):
    """Compile a library; host uses Cargo, cross targets reuse the wheel toolchain."""
    source = _build_source(root)
    host = PlatformInfo.detect()
    selected = target or host.target
    if selected not in RUST_TRIPLES:
        raise ValueError(f"不支持的 Rust 平台：{selected}")
    output = artifact_directory(root, output)
    if selected != host.target:
        # Maturin manages target linkers/ABI configuration; retain only the library for build.
        with tempfile.TemporaryDirectory(prefix="repo-agent-cross-library-") as temporary:
            wheel = build_rust_wheel(
                root, python, temporary, offline=offline, target=selected, **wheel_options
            )
            with zipfile.ZipFile(wheel) as archive:
                libraries = [name for name in archive.namelist() if name.endswith(".so")]
                if len(libraries) != 1:
                    raise ValueError("Rust wheel 未包含唯一扩展动态库")
                suffix = "dylib" if selected.startswith("macos-") else "so"
                library = Path(temporary) / f"librust_backend.{suffix}"
                library.write_bytes(archive.read(libraries[0]))
            return _publish(library, output / selected)
    with tempfile.TemporaryDirectory(prefix="repo-agent-rust-") as temporary:
        target = Path(temporary) / "target"
        command = [
            "cargo",
            "build",
            "--manifest-path",
            str(source / "Cargo.toml"),
            "--release",
            "--locked",
            "--lib",
            "--features",
            "extension-module",
        ]
        if offline:
            command.append("--offline")
        env = {**os.environ, "CARGO_TARGET_DIR": str(target), "PYO3_PYTHON": str(python)}
        # Specify the host explicitly so user Cargo cross-target defaults cannot mislabel output.
        triple = RUST_TRIPLES[selected]
        command.extend(["--target", triple])
        subprocess.run(command, env=env, cwd=temporary, check=True)
        suffix = "dylib" if host.target.startswith("macos-") else "so"
        library = target / triple / "release" / f"librust_backend.{suffix}"
        return _publish(library, output / host.target)


def build_rust_wheel(
    root,
    python,
    output=None,
    *,
    offline=False,
    wheelhouse=None,
    compatibility=None,
    build_isolation=True,
    target=None,
):
    """Build outside the workspace; publish only this invocation's completed wheel."""
    source = _build_source(root)
    output = artifact_directory(root, output)
    flags = (
        ["--no-index"]
        if offline and not build_isolation and wheelhouse is None
        else source_flags(wheelhouse, offline=offline)
    )
    with tempfile.TemporaryDirectory(prefix="repo-agent-rust-") as temporary:
        staged = Path(temporary) / "wheels"
        staged.mkdir()
        env = {**os.environ, "CARGO_TARGET_DIR": str(Path(temporary) / "target")}
        if offline:
            env["CARGO_NET_OFFLINE"] = "true"
        arguments = ["--locked"]
        if target is not None:
            if target not in RUST_TRIPLES:
                raise ValueError(f"不支持的 Rust 平台：{target}")
            arguments.extend(["--target", RUST_TRIPLES[target]])
            if target != PlatformInfo.detect().target:
                env["PYO3_CROSS"] = "1"
                env["PYO3_NO_PYTHON"] = "1"
                if target.startswith("linux-"):
                    arguments.append("--zig")
                    env.setdefault("CARGO_ZIGBUILD_CACHE_DIR", str(Path(temporary) / "zig-tools"))
                    env.setdefault("ZIG_GLOBAL_CACHE_DIR", str(Path(temporary) / "zig-cache"))
            if target.startswith("macos-"):
                env["MACOSX_DEPLOYMENT_TARGET"] = "11.0" if target.endswith("arm64") else "10.15"
            elif compatibility is None:
                compatibility = "manylinux_2_28"
        if compatibility is not None:
            arguments.extend(["--compatibility", compatibility])
        env["MATURIN_PEP517_ARGS"] = shlex.join(arguments)
        run_download(
            [
                str(python),
                "-B",
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-cache-dir",
                "--wheel-dir",
                str(staged),
                *([] if build_isolation else ["--no-build-isolation"]),
                *flags,
                str(source.resolve()),
            ],
            label="编译可选 Rust 扫描扩展",
            env=env,
            cwd=temporary,
        )
        wheels = list(staged.glob(f"rust_backend-{output.name}-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Rust 构建未生成唯一的扩展 wheel")
        return _publish(wheels[0], output)


def install_rust_extension(root, *, release=None, offline=False, wheelhouse=None):
    """Best effort after the core install commits. Never compile a release on the user host."""
    try:
        if PlatformInfo.detect().target not in RUST_TARGETS:
            return False
        root = Path(root)
        python = installed_python(root)
        with tempfile.TemporaryDirectory(prefix="repo-agent-rust-wheel-") as temporary:
            if release is not None:
                name = release.get("rust_wheel")
                if name is None:
                    print(
                        "[INFO] 此发行包未附带 Rust 扩展；若已配置 rust，请改为 python 或补装扩展。"
                    )
                    return False
                wheel = root / name
                # Only install a binary covered by the release integrity manifest.
                if release["files"].get(name) != digest(wheel):
                    raise ValueError("Rust 扩展 wheel 校验失败")
            else:
                wheel = build_rust_wheel(root, python, offline=offline, wheelhouse=wheelhouse)
            run_download(
                [
                    str(python),
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--force-reinstall",
                    str(wheel),
                ],
                label="安装可选 Rust 扫描扩展",
                cwd=root,
            )
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import rust_backend; "
                    "assert rust_backend.API_VERSION == 1; "
                    "assert callable(rust_backend.scan); "
                    "assert rust_backend.FILESYSTEM_API_VERSION == 1",
                ],
                cwd=temporary,
                check=True,
                capture_output=True,
                timeout=30,
            )
        print("[OK] Rust 扫描扩展已安装并验证；AGENT_NATIVE_SCANNER=rust 可选择使用。")
        return True
    except KeyboardInterrupt:
        print("[WARN] 已取消 Rust 扩展安装；Agent 核心安装保留。")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Do not echo subprocess output, which may contain credentials or proxy URLs.
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"[WARN] Rust 扩展未就绪：{detail}；Agent 核心安装不受影响。")
    print("默认 Python 扫描器仍可使用；若已显式配置 rust，请改为 python 或补装扩展。")
    return False
