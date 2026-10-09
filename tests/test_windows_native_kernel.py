"""Opt-in complete Windows native startup/tool/publication acceptance."""

import json
import os
import subprocess
import venv
from pathlib import Path

import pytest

from host_support.windows_isolation import WindowsIsolationAPI
from host_support.windows_processes import WindowsIsolation
from host_support.windows_recovery import recover_profiles
from host_support.windows_security import PrivateWindowsSecurity
from sandbox.native import create_native_backend

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.getenv("RUN_WINDOWS_ISOLATION_TESTS") != "1",
    reason="Requires real Windows x64 LPAC/Job APIs and explicit opt-in",
)


def test_native_startup_python_worker_git_and_publication(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr("sandbox.windows_native.app_directory", lambda _: state)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("secret")
    (root / "sample.py").write_text("def answer():\n    return 42\n")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    backend = create_native_backend(root, profile="standard")
    try:
        assert backend.preflight_metrics["isolation_verified"]
        result = backend.execute(
            root,
            "run_python",
            {
                "code": (
                    "import pathlib, json, os; "
                    "p=pathlib.Path('.'); "
                    "assert not (p/'.env').exists(); "
                    "(p/'result.txt').write_text('published'); "
                    "print(json.dumps({'cwd':str(p.resolve())}))"
                )
            },
        )
        assert result.success, result
        assert (root / "result.txt").read_text() == "published"
        assert (root / ".env").read_text() == "secret"
        assert json.loads(result.data["stdout"])["cwd"] != str(root)
        assert backend.execution_context()["last_writeback"]["files"] == ["result.txt"]
        report = backend.execute(root, "get_execution_environment", {})
        assert report.success, report
        assert report.data["execution"]["writeback_mode"] == "per_call_after_cleanup"
        git = backend.execute(root, "git_status", {})
        assert git.success, git
        symbols = backend.execute(root, "get_symbols", {"path": "sample.py"})
        assert symbols.success and "answer" in str(symbols.data), symbols
        assert backend.healthy
        assert not list(backend.windows_state_root.glob("*.json"))
    finally:
        backend.close()


def test_native_relocates_project_venv_without_changing_original(tmp_path, monkeypatch):
    monkeypatch.setattr("sandbox.windows_native.app_directory", lambda _: tmp_path / "state")
    root = tmp_path / "project"
    root.mkdir()
    environment = root / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    (environment / "Lib/site-packages/private_marker.py").write_text("VALUE = 73")
    original = (environment / "pyvenv.cfg").read_bytes()
    backend = create_native_backend(root, project_python=str(environment / "Scripts/python.exe"))
    try:
        result = backend.execute(
            root,
            "run_python",
            {
                "code": (
                    "import private_marker, sys; print(private_marker.VALUE); print(sys.prefix)"
                )
            },
        )
        assert result.success and result.data["stdout"].splitlines()[0] == "73", result
        assert str(environment) not in result.data["stdout"]
        assert (environment / "pyvenv.cfg").read_bytes() == original
    finally:
        backend.close()


def test_recovery_keeps_retained_work_and_never_follows_junctions(tmp_path):
    state, outside = tmp_path / "state", tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("host data")
    api = WindowsIsolationAPI()
    PrivateWindowsSecurity(api).set_acl(state)
    with WindowsIsolation(recovery_root=state) as scope:
        directory, identity, name = (
            scope.profile.directory,
            scope.lease.identity,
            scope.profile.name,
        )
        subprocess.run(
            [
                str(Path(api.windows_directory()) / "System32/cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(directory / "junction"),
                str(outside),
            ],
            check=True,
            capture_output=True,
        )
        os.link(marker, directory / "hardlink.txt")
        (directory / "readonly.txt").write_text("scratch")
        os.chmod(directory / "readonly.txt", 0o444)
        scope.retain("test retained workspace")
        assert recover_profiles(state, api)["active"] == [name]
    assert recover_profiles(state, api)["retained"] == [name]
    report = recover_profiles(state, api, discard=identity)
    assert report["removed"] == [name] and not report["failed"], report
    assert not directory.exists()
    assert marker.read_text() == "host data"
