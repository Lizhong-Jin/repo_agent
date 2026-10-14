"""Opt-in Windows native contracts; full functional acceptance remains explicit."""

import json
import os
import subprocess
import sys
import venv
from dataclasses import asdict
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


def require_success(result, stage):
    if not result.success:
        # ToolResult repr is shortened by pytest, hiding the end of tracebacks.
        # Process capture already bounds stdout/stderr; emit it as text instead.
        pytest.fail(
            f"{stage}:\n{json.dumps(asdict(result), ensure_ascii=True, indent=2)}", pytrace=False
        )


def record_capability(stage, status, *, reason=None):
    if destination := os.getenv("WINDOWS_NATIVE_CAPABILITY_REPORT"):
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stage": stage, "status": status, "reason": reason}) + "\n")


def require_capability_outcome(result, backend, capability, stage):
    """Only known LPAC incompatibility is an accepted rejection, never arbitrary failure."""
    if result.success or os.getenv("REQUIRE_WINDOWS_NATIVE_FULL_TOOLS") == "1":
        require_success(result, stage)
        record_capability(stage, "available")
        print(f"Windows native {stage}: available")
        return True
    report = result.data.get("capability_report", {})
    accepted = (
        result.error_code == "NATIVE_CAPABILITY_UNAVAILABLE"
        and report.get("status") == "unavailable"
        and report.get("reason") == "lpac_compatibility"
        and report.get("diagnostics", {}).get("cleanup_status") == "confirmed"
        and result.data.get("capability") == capability
        and "--sandbox docker" in (result.error or "")
        and "--new-session" in (result.error or "")
        and backend.healthy
        and result.data.get("execution_allowed") is False
    )
    if not accepted:
        # pytest abbreviates dicts in assertions, concealing the actual probe
        # exit code/stderr. Emit the already bounded result as explicit text.
        require_success(result, stage)
    record_capability(stage, "unavailable", reason="lpac_compatibility")
    print(f"Windows native {stage}: unavailable (lpac_compatibility), cleanup confirmed")
    return False


@pytest.mark.parametrize("stage", ["python", "git", "lsp", "all"])
def test_native_startup_python_worker_git_and_publication(tmp_path, monkeypatch, stage):
    state = tmp_path / "state"
    monkeypatch.setattr("sandbox.windows_native.app_directory", lambda _: state)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("secret")
    (root / "sample.py").write_text("def answer():\n    return 42\n")
    if stage in {"git", "all"}:

        def git(*args):
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

        git("init")
        git("add", "sample.py")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "initial",
        )
        git("config", "core.fsmonitor", "should-never-run.exe")
        git("config", "filter.trap.process", "should-never-run.exe")
        git("config", "diff.trap.command", "should-never-run.exe")
        (root / ".gitattributes").write_text("sample.py filter=trap diff=trap\n")
        (root / "sample.py").write_text("def answer():\n    return 43\n")
    else:

        def unexpected_git(*args, **kwargs):
            pytest.fail("Python and LSP must work without Git installed")

        monkeypatch.setattr("sandbox.windows_native.find_windows_executable", unexpected_git)
    backend = create_native_backend(root, profile="standard", project_python=sys.executable)
    try:
        assert backend.preflight_metrics["isolation_verified"]
        if stage in {"python", "all"}:
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
            require_success(result, "run_python")
            assert (root / "result.txt").read_text() == "published"
            assert (root / ".env").read_text() == "secret"
            assert json.loads(result.data["stdout"])["cwd"] != str(root)
            assert backend.execution_context()["last_writeback"]["files"] == ["result.txt"]
            report = backend.execute(root, "get_execution_environment", {})
            require_success(report, "get_execution_environment")
            assert report.data["execution"]["writeback_mode"] == "per_call_after_cleanup"
        if stage in {"git", "all"}:
            index_before = (root / ".git/index").read_bytes()
            for name, arguments in (
                ("git_status", {}),
                ("git_diff", {}),
                ("git_log", {}),
                ("git_show", {"path": "sample.py"}),
            ):
                query = backend.execute(root, name, arguments)
                available = require_capability_outcome(query, backend, "git", name)
                if available and name == "git_show":
                    assert "return 42" in str(query.data)
                if available and name == "git_diff":
                    assert "return 43" in str(query.data)
            assert (root / ".git/index").read_bytes() == index_before
            assert (root / "sample.py").read_text().endswith("return 43\n")
        if stage in {"lsp", "all"}:
            symbols = backend.execute(root, "get_symbols", {"path": "sample.py"})
            if require_capability_outcome(symbols, backend, "lsp", "get_symbols"):
                assert "answer" in str(symbols.data), symbols.data
            else:
                repeated = backend.execute(root, "get_symbols", {"path": "sample.py"})
                assert repeated == symbols
                assert backend.last_tool_metrics["runs"] == []
        require_success(
            backend.execute(root, "read_file", {"reads": [{"path": "sample.py"}]}),
            "read after probe",
        )
        require_success(
            backend.execute(root, "write_file", {"path": "capability-ok.txt", "content": "ok"}),
            "write after probe",
        )
        assert (root / "capability-ok.txt").read_text() == "ok"
        assert not (root / "sample.py").read_text().startswith("def native_capability_example")
        assert backend.healthy
        assert not list(backend.windows_state_root.glob("*.json"))
    finally:
        if destination := os.getenv("WINDOWS_NATIVE_CAPABILITY_REPORT_DIR"):
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{stage}.json").write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "healthy": backend.healthy,
                        "capabilities": backend.execution_context()["capabilities"],
                    },
                    ensure_ascii=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
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
        require_success(result, "relocated venv run_python")
        assert result.data["stdout"].splitlines()[0] == "73", result.data
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
