"""Target dispatch, missing-tool diagnostics, and cross-wheel command composition."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support.platforms import PlatformInfo
from installer import rust_extension, rust_targets
from scripts import build_rust


def test_default_build_attempts_all_available_targets_and_reports_missing(
    monkeypatch, tmp_path, capsys
):
    calls = []
    monkeypatch.setattr(
        build_rust,
        "prerequisites",
        lambda target: ["rustup target add ..."] if target == "macos-x86_64" else [],
    )
    monkeypatch.setattr(
        build_rust,
        "build_rust_wheel",
        lambda *a, **kw: calls.append(kw["target"]) or tmp_path / "built.whl",
    )
    assert build_rust.main(["wheel"]) == 1
    assert set(calls) == set(rust_targets.RUST_TRIPLES) - {"macos-x86_64"}
    assert "rustup target add" in capsys.readouterr().out


def test_selected_target_failure_does_not_stop_remaining_builds(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(build_rust, "prerequisites", lambda target: [])

    def build(*args, **kwargs):
        calls.append(kwargs["target"])
        if len(calls) == 1:
            raise ValueError("failed")
        return tmp_path / "built.whl"

    monkeypatch.setattr(build_rust, "build_rust_wheel", build)
    assert build_rust.main(["wheel", "--target", "linux-x86_64", "--target", "macos-arm64"]) == 1
    assert calls == ["linux-x86_64", "macos-arm64"]


def test_check_never_builds_and_host_alias_is_resolved(monkeypatch):
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("macos", "arm64"))
    monkeypatch.setattr(
        build_rust, "prerequisites", lambda target: [] if target == "macos-arm64" else ["bad"]
    )
    monkeypatch.setattr(
        build_rust, "build_rust_library", lambda *a, **kw: pytest.fail("check built")
    )
    assert build_rust.main(["build", "--target", "host", "--check"]) == 0


def test_prerequisite_diagnostics_do_not_install_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("macos", "arm64"))
    monkeypatch.setattr(rust_targets.shutil, "which", lambda name: None if name == "zig" else name)
    monkeypatch.setattr(
        rust_targets.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=str(tmp_path))
    )
    messages = rust_targets.prerequisites("linux-arm64")
    assert any("rustup target add aarch64-unknown-linux-gnu" in message for message in messages)
    assert any("Zig" in message for message in messages)


@pytest.mark.parametrize("target,flag", [("linux-x86_64", "--zig"), ("macos-x86_64", "--target")])
def test_cross_wheel_uses_target_and_abi3_without_host_python(monkeypatch, tmp_path, target, flag):
    (tmp_path / "rust").mkdir()
    (tmp_path / "rust/Cargo.toml").touch()
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("macos", "arm64"))
    monkeypatch.setattr(rust_extension.shutil, "which", lambda name: name)

    def build(command, *, env, **kwargs):
        assert env["PYO3_CROSS"] == env["PYO3_NO_PYTHON"] == "1"
        assert rust_targets.RUST_TRIPLES[target] in env["MATURIN_PEP517_ARGS"]
        assert flag in env["MATURIN_PEP517_ARGS"]
        if target.startswith("linux"):
            assert "manylinux_2_28" in env["MATURIN_PEP517_ARGS"]
        else:
            assert env["MACOSX_DEPLOYMENT_TARGET"] == "10.15"
        output = Path(command[command.index("--wheel-dir") + 1])
        (output / "repo_agent_policy_scan-0.1.0-cp311-abi3-test.whl").write_bytes(b"wheel")

    monkeypatch.setattr(rust_extension, "run_download", build)
    assert (
        rust_extension.build_rust_wheel(tmp_path, "python", target=target).read_bytes() == b"wheel"
    )


@pytest.mark.parametrize("command,expected", [("build", 0), ("wheel", 1)])
def test_missing_maturin_is_reported_only_when_needed(
    monkeypatch, tmp_path, capsys, command, expected
):
    monkeypatch.setattr(PlatformInfo, "detect", lambda: PlatformInfo("macos", "arm64"))
    monkeypatch.setattr(build_rust, "prerequisites", lambda target: [])
    monkeypatch.setattr(
        build_rust.importlib.util, "find_spec", lambda name: None if name == "maturin" else object()
    )
    monkeypatch.setattr(build_rust, "build_rust_library", lambda *a, **kw: tmp_path / "library")
    monkeypatch.setattr(
        build_rust, "build_rust_wheel", lambda *a, **kw: pytest.fail("missing tool invoked")
    )
    assert build_rust.main([command, "--target", "host", "--no-build-isolation"]) == expected
    if expected:
        assert "pip install" in capsys.readouterr().out
