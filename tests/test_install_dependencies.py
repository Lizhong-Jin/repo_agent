"""Mode-specific install contracts; never invoke real Homebrew or download toolchains."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import doctor
from installer import dependencies, install_network, setup
from installer.installation import COMMANDS, begin_install, load_record, prepare_venv, save_record
from sandbox import lsp_smoke
from sandbox.native import NativeBackend


@pytest.fixture(autouse=True)
def download_executor_for_installer_mocks(monkeypatch):
    monkeypatch.setattr(
        install_network, "execute", lambda command, **kw: subprocess.run(command, **kw)
    )


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    (root / ".env.example").write_text("LLM_MODEL=\n")
    record = begin_install(root)
    prepare_venv(record)
    (root / ".venv/bin").mkdir(exist_ok=True)
    for name in COMMANDS:
        (root / ".venv/bin" / name).write_text("old entry")
    (root / ".venv/old-marker").write_text("preserve")
    record["status"] = "installed"
    save_record(record)
    return root


def test_shared_native_path_includes_managed_servers(tmp_path):
    python = tmp_path / ".venv/bin/python"
    backend = NativeBackend.__new__(NativeBackend)
    backend.python = python
    env = backend._environment(tmp_path / "scratch")
    assert env["PATH"] == dependencies.tool_path(python)
    assert str(tmp_path / ".venv/lsp/node_modules/.bin") in env["PATH"]
    assert env["GOTOOLCHAIN"] == "local" and env["GOPROXY"] == "off"


def test_missing_toolchains_are_filled_by_brew(monkeypatch, tmp_path):
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="darwin"))
    commands = []
    available = ["python"]
    monkeypatch.setattr(dependencies, "brew_executable", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(dependencies, "toolchain_report", lambda *a: ([], available.copy()))

    def run(command, **kwargs):
        commands.append(command)
        available.extend(["typescript", "go", "cpp"])

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    assert dependencies.prepare_toolchains(tmp_path / ".venv/bin/python") == list(
        dependencies.LANGUAGES
    )
    assert commands == [["/opt/homebrew/bin/brew", "install", "node", "go", "llvm"]]


def test_python_only_needs_no_homebrew(monkeypatch, tmp_path):
    monkeypatch.setattr(dependencies, "brew_executable", lambda: None)
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("no external command expected"),
    )
    assert dependencies.prepare_toolchains(tmp_path / ".venv/bin/python", "python") == ["python"]


def test_check_reports_missing_brew_without_installing(monkeypatch, tmp_path):
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(dependencies, "brew_executable", lambda: None)
    monkeypatch.setattr(dependencies, "toolchain_report", lambda *a: ([], ["python"]))
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("check is read-only"),
    )
    rows = dependencies.preparation_report(tmp_path / ".venv/bin/python")
    assert any(row[0] == "ERROR" and row[1] == "Homebrew" for row in rows)
    with pytest.raises(ValueError, match="Homebrew"):
        dependencies.prepare_toolchains(tmp_path / ".venv/bin/python")


def test_existing_go_that_is_too_old_is_not_marked_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(dependencies.shutil, "which", lambda name, **kw: "/bin/" + name)
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="go version go1.23.1 darwin/arm64"),
    )
    rows, available = dependencies.toolchain_report(tmp_path / ".venv/bin/python", "go")
    assert available == ["python"]
    assert any(level == "ERROR" and name == "go" for level, name, _ in rows)


def test_language_servers_install_in_owned_virtualenv(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(dependencies.shutil, "which", lambda name, **kw: "/usr/local/bin/" + name)
    monkeypatch.setattr(
        dependencies.subprocess, "run", lambda args, **kw: commands.append((args, kw))
    )
    dependencies.install_language_servers(tmp_path, ["python", "typescript", "go", "cpp"])
    npm, go = commands
    assert "--global" not in npm[0]
    assert npm[0][npm[0].index("--prefix") + 1] == str(tmp_path / ".venv/lsp")
    assert "--ignore-scripts" in npm[0]
    assert go[1]["env"]["GOBIN"] == str(tmp_path / ".venv/bin")
    assert go[1]["env"]["GOTOOLCHAIN"] == "local"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "language-install",
        "language-interrupt",
        "middle-language",
        "optional-preflight",
        "smoke",
    ],
)
def test_native_install_adds_lsp_and_rolls_back_failures(installation, monkeypatch, failure):
    root = installation
    commands = []
    checks = []
    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: checks.append(kw) or [])
    monkeypatch.setattr(setup, "native_preflight", lambda: [])
    monkeypatch.setattr(
        setup,
        "preparation_report",
        lambda *a: [("ERROR", "Homebrew", "missing")] if failure == "optional-preflight" else [],
    )
    monkeypatch.setattr(
        setup,
        "language_status",
        lambda root: [
            {"language": name, "toolchain": True, "service": name == "python"}
            for name in dependencies.LANGUAGES
        ],
    )
    monkeypatch.setattr(setup, "print_language_status", lambda root: None)

    def install_servers(root, names):
        assert load_record(root)["status"] == "installed"
        assert not (root / ".repo-agent-install-transaction").exists()
        assert (Path.home() / ".local/bin/repo-agent").resolve() == root / ".venv/bin/repo-agent"
        if failure == "optional-preflight":
            raise ValueError("缺少 Homebrew")
        if failure == "middle-language" and names == ["go"]:
            raise subprocess.CalledProcessError(1, ["go", "install"])
        if failure == "language-install":
            raise subprocess.CalledProcessError(1, ["npm", "install"])
        if failure == "language-interrupt":
            raise KeyboardInterrupt
        record = load_record(root)
        record["languages"] += names
        record["pending_languages"] = [
            name for name in record["pending_languages"] if name not in names
        ]
        save_record(record)

    monkeypatch.setattr(setup, "install_missing", install_servers)
    monkeypatch.setattr(
        setup,
        "service_report",
        lambda *a, **kw: [("ERROR" if failure == "smoke" else "OK", "test", "result")],
    )

    def run(command, **kwargs):
        commands.append(command)
        if command[1:4] == ["-m", "pip", "install"]:
            (root / ".venv/bin").mkdir(exist_ok=True)
            for name in COMMANDS:
                (root / ".venv/bin" / name).write_text("new entry")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup",
            "--bootstrap",
            "--agent-home",
            str(root),
            "--mode",
            "native",
            "--no-path",
            "--with-toolchains",
            "--languages",
            "all" if failure == "middle-language" else "typescript",
        ],
    )
    if failure == "smoke":
        with pytest.raises(SystemExit):
            setup.main()
        assert (root / ".venv/old-marker").exists()
    else:
        setup.main()
        assert load_record(root)["languages"] == (
            ["python", "typescript", "cpp"]
            if failure == "middle-language"
            else ["python"] if failure else ["python", "typescript"]
        )
        assert load_record(root)["pending_languages"] == (
            ["go"] if failure == "middle-language" else ["typescript"] if failure else []
        )
        assert not (root / ".venv/old-marker").exists()
        assert load_record(root)["mode"] == "native"
        assert not load_record(root)["uses_default_image"]
    locked = next(command for command in commands if "--require-hashes" in command)
    assert str(root / "requirements-dev.lock") in locked
    assert str(root / "requirements-build.lock") in locked
    assert all(not call["docker"] for call in checks)
    assert any(str(root) + "[lsp]" in command for command in commands)
    assert any(command[1:] == ["-m", "pip", "check"] for command in commands)
    assert not any("sandbox.build" in command for command in commands)


def test_install_check_does_not_install_or_require_docker(installation, monkeypatch):
    monkeypatch.setattr(
        setup,
        "environment_report",
        lambda root, docker: [] if not docker else pytest.fail("Docker requested"),
    )
    monkeypatch.setattr(setup, "native_preflight", lambda: [])
    monkeypatch.setattr(setup, "preparation_report", lambda *a: [("WARN", "Go", "will install")])
    monkeypatch.setattr(setup, "install_missing", lambda *a: pytest.fail("check cannot install"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup", "--agent-home", str(installation), "--mode", "native", "--check"],
    )
    setup.main()
    assert (installation / ".venv/old-marker").exists()


def test_mode_is_read_from_installation_not_working_directory(installation, tmp_path, monkeypatch):
    record = load_record(installation)
    record["mode"] = "docker"
    save_record(record)
    other = tmp_path / "project"
    other.mkdir()
    (other / ".repo-agent-install.json").write_text('{"mode":"local"}')
    monkeypatch.chdir(other)
    assert dependencies.available_mode(installation) == "docker"
    assert dependencies.available_mode(other) == "native"


def test_non_native_platform_is_rejected(monkeypatch):
    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="win32"))
    assert dependencies.native_preflight()[0][0] == "ERROR"


def test_doctor_native_uses_actual_probe_and_surfaces_failures(installation, tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr(doctor, "native_preflight", lambda: [])
    monkeypatch.setattr(doctor, "probe", lambda *a, **kw: SimpleNamespace(returncode=0))
    seen = []

    def service(root, **kwargs):
        seen.append(kwargs)
        return [("ERROR", "example.py", "missing server")]

    monkeypatch.setattr(doctor, "service_report", service)
    rows = doctor.diagnose(installation, tmp_path, mode="native")
    assert seen == [{"mode": "native", "languages": None}]
    assert ("ERROR", "example.py", "missing server") in rows


def test_smoke_native_exercises_backend_and_cleans_it(monkeypatch):
    calls, closed = [], []

    class Backend:
        def __init__(self, workspace):
            assert (workspace / "py/example.py").exists()

        def tools(self):
            def execute(args):
                calls.append(args)
                return SimpleNamespace(success=True, data={"symbols": [{"name": "example"}]})

            return [
                SimpleNamespace(definition=SimpleNamespace(name="get_symbols"), execute=execute)
            ]

        def close(self):
            closed.append(True)

    monkeypatch.setattr("sandbox.native.NativeBackend", Backend)
    rows = lsp_smoke.check_services(mode="native", languages=["python"])
    assert rows == [("OK", "example.py", "符号查询通过")]
    assert calls == [{"path": "py/example.py"}] and closed == [True]


def test_smoke_never_reports_success_without_expected_symbol(monkeypatch):
    tool = SimpleNamespace(
        definition=SimpleNamespace(name="get_symbols"),
        execute=lambda args: SimpleNamespace(success=True, data={"symbols": []}),
    )
    monkeypatch.setattr(lsp_smoke, "create_default_tools", lambda *a, **kw: [tool])
    assert lsp_smoke.check_services(languages=["python"])[0][0] == "ERROR"


def test_smoke_reports_underlying_permission_failure(monkeypatch):
    denied = "/lib/modules/6.6.87.2-microsoft-standard-WSL2/lost+found"

    def backend(root):
        raise PermissionError(13, "Permission denied", denied)

    monkeypatch.setattr("sandbox.native.NativeBackend", backend)
    row = lsp_smoke.check_services(mode="native", languages=["python"])[0]
    assert row[:2] == ("ERROR", "原生沙箱")
    assert "PermissionError" in row[2] and denied in row[2] and "Permission denied" in row[2]


def test_smoke_bounds_and_sanitizes_failure_detail(monkeypatch):
    def backend(root):
        raise ValueError("namespace failed\n\x1b[2J" + "x" * 2000)

    monkeypatch.setattr("sandbox.native.NativeBackend", backend)
    detail = lsp_smoke.check_services(mode="native", languages=["python"])[0][2]
    assert "ValueError" in detail and "namespace failed" in detail
    assert "\n" not in detail and "\x1b" not in detail and len(detail) < 1700


def test_docker_diagnostic_is_offline_without_mounts_and_always_cleans(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    rows = dependencies.service_report(tmp_path, mode="docker")
    assert rows[0][0] == "ERROR"
    assert "--network=none" in calls[0] and "--pull=never" in calls[0]
    assert "--mount" not in calls[0] and "-v" not in calls[0]
    assert calls[1][:3] == ["docker", "rm", "-f"]
    assert calls[1][3] == calls[0][calls[0].index("--name") + 1]


def test_diagnostic_malformed_output_does_not_leak_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=1, stdout="private-secret", stderr="private-secret"
        ),
    )
    rows = dependencies.service_report(tmp_path, mode="native")
    assert rows[0][0] == "ERROR" and "private-secret" not in str(rows)


def test_versions_match_dockerfile():
    text = (Path(__file__).resolve().parents[1] / "sandbox/Dockerfile").read_text()
    for version in (
        dependencies.GOPLS,
    ):
        assert version in text
