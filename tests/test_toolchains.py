"""Optional installation, native dependency status and targeted repair contracts."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from installer import dependencies, install_network, setup, toolchains
from installer.installation import COMMANDS, begin_install, load_record, prepare_venv, save_record


def states(services=()):
    return [
        {"language": name, "toolchain": True, "service": name in services}
        for name in dependencies.LANGUAGES
    ]


@pytest.fixture(autouse=True)
def download_executor_for_installer_mocks(monkeypatch):
    monkeypatch.setattr(
        install_network, "execute", lambda command, **kw: subprocess.run(command, **kw)
    )


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (root / ".env.example").write_text("LLM_MODEL=\n")
    record = begin_install(root)
    prepare_venv(record)
    (root / ".venv/bin").mkdir()
    (root / ".venv/bin/python").write_text("old python")
    for name in COMMANDS:
        (root / ".venv/bin" / name).write_text("old entry")
    record.update(status="installed", mode="native", languages=["python"])
    save_record(record)
    return root


@pytest.mark.parametrize("answer,expected", [("", False), ("n", False), ("yes", True), ("Y", True)])
def test_prompt_defaults_to_skip(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert setup.choose_toolchains(None) is expected


def test_eof_skips_and_explicit_flags_never_prompt(monkeypatch):
    def eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert setup.choose_toolchains(None) is False
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected prompt"))
    assert setup.choose_toolchains(True) is True
    assert setup.choose_toolchains(False) is False


@pytest.mark.parametrize("preserve", [False, True])
def test_skip_installs_python_without_brew_or_server_downloads(installed, monkeypatch, preserve):
    root = installed
    record = load_record(root)
    if preserve:
        (root / ".venv/bin/gopls").write_text("keep gopls")
        (root / ".venv/lsp").mkdir()
        (root / ".venv/lsp/marker").write_text("keep npm")
        record["languages"] = ["python", "go"]
        save_record(record)
    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr(setup, "native_preflight", lambda: [])
    monkeypatch.setattr(setup, "preparation_report", lambda *a: [("ERROR", "Homebrew", "missing")])
    monkeypatch.setattr(
        setup,
        "install_missing",
        lambda *a: pytest.fail("skip must not install tools"),
    )
    monkeypatch.setattr(
        setup,
        "language_status",
        lambda root: states(["python", "go"] if preserve else ["python"]),
    )
    monkeypatch.setattr(setup, "print_language_status", lambda root: None)
    requested = []

    def supplement(root, names):
        assert preserve
        requested.extend(names)
        record = load_record(root)
        record["languages"] += names
        save_record(record)

    monkeypatch.setattr(setup, "install_missing", supplement)
    verified = []
    monkeypatch.setattr(
        setup,
        "service_report",
        lambda root, **kw: verified.extend(kw["languages"]) or [],
    )

    def run(command, **kw):
        if command[1:4] == ["-m", "pip", "install"]:
            if "--require-hashes" in command:
                assert str(root / "requirements-lsp.lock") in command
                return SimpleNamespace(returncode=0)
            assert str(root) + "[lsp]" in command
            (root / ".venv/bin").mkdir()
            for name in COMMANDS:
                (root / ".venv/bin" / name).write_text("new entry")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    monkeypatch.setattr(
        sys, "argv", ["setup", "--bootstrap", "--agent-home", str(root), "--no-path"]
    )
    setup.main()
    assert requested == (["go"] if preserve else [])
    assert verified == ["python"]
    assert load_record(root)["languages"] == (["python", "go"] if preserve else ["python"])
    if preserve:
        assert (root / ".venv/bin/gopls").read_text() == "keep gopls"
        assert (root / ".venv/lsp/marker").read_text() == "keep npm"


def test_list_distinguishes_go_from_gopls_and_is_offline(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        dependencies.shutil,
        "which",
        lambda name, **kw: None if name == "gopls" else "/bin/" + name,
    )

    def run(command, **kw):
        calls.append(command)
        assert "install" not in command
        return SimpleNamespace(returncode=0, stdout="go version go1.27.1 darwin/arm64")

    monkeypatch.setattr(dependencies.subprocess, "run", run)
    rows = dependencies.print_language_status(tmp_path)
    go = next(row for row in rows if row["language"] == "go")
    assert go["toolchain"] is True and go["service"] is False
    output = capsys.readouterr().out
    assert "Go 1.25+: 已安装 | gopls: 缺失或不可用" in output
    assert "repo-agent toolchains install go" in output
    assert {row["language"] for row in rows} == set(dependencies.LANGUAGES)


def test_missing_python_is_not_listed_as_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(dependencies.shutil, "which", lambda *a, **kw: None)
    monkeypatch.setattr(dependencies, "command_works", lambda *a: False)
    row = dependencies.language_status(tmp_path)[0]
    assert not row["toolchain"] and not row["service"]


@pytest.mark.parametrize("existing", [False, True])
def test_supplement_reuses_existing_go_and_only_installs_missing_gopls(
    installed, monkeypatch, existing
):
    monkeypatch.setattr(toolchains, "native_preflight", lambda: [])
    monkeypatch.setattr(
        toolchains,
        "language_status",
        lambda root: states(["python", "go"] if existing else ["python"]),
    )
    monkeypatch.setattr(
        toolchains,
        "prepare_toolchains",
        lambda *a: pytest.fail("existing Go must be reused"),
    )
    downloads = []
    monkeypatch.setattr(
        toolchains, "install_server", lambda root, language: downloads.append(language)
    )
    monkeypatch.setattr(toolchains, "service_report", lambda *a, **kw: [])
    toolchains.install_missing(installed, ["go"])
    assert downloads == ([] if existing else ["go"])
    assert load_record(installed)["languages"] == ["python", "go"]
    assert (installed / ".venv/bin/python").read_text() == "old python"


def test_supplement_failure_does_not_record_success(installed, monkeypatch):
    before = load_record(installed)
    monkeypatch.setattr(toolchains, "native_preflight", lambda: [])
    monkeypatch.setattr(toolchains, "language_status", lambda root: states(["python"]))

    def fail(*a):
        raise subprocess.CalledProcessError(1, ["go", "install"])

    monkeypatch.setattr(toolchains, "install_server", fail)
    with pytest.raises(subprocess.CalledProcessError):
        toolchains.install_missing(installed, ["go"])
    assert load_record(installed) == before


@pytest.mark.parametrize("language", ["go", "typescript"])
@pytest.mark.parametrize("failure", [None, "download", "verification"])
def test_staged_service_replacement_preserves_original_on_failure(
    installed, monkeypatch, language, failure
):
    target = installed / ".venv" / ("bin/gopls" if language == "go" else "lsp")
    if language == "typescript":
        target.mkdir()
        (target / "marker").write_text("old")
    else:
        target.write_text("old")

    def download(staging, names):
        assert names == [language]
        if failure == "download":
            raise subprocess.CalledProcessError(1, ["download"])
        if language == "typescript":
            (staging / ".venv/lsp").mkdir()
            (staging / ".venv/lsp/marker").write_text("new")
        else:
            (staging / ".venv/bin/gopls").write_text("new")

    monkeypatch.setattr(toolchains, "install_language_servers", download)
    monkeypatch.setattr(
        toolchains,
        "service_report",
        lambda *a, **kw: [("ERROR", "service", "failed")] if failure == "verification" else [],
    )
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            toolchains.install_server(installed, language)
    else:
        toolchains.install_server(installed, language)
    actual = target / "marker" if language == "typescript" else target
    assert actual.read_text() == ("old" if failure else "new")
    assert not list((installed / ".venv").glob(".toolchain-*"))


def test_incomplete_install_is_not_modified(installed, monkeypatch):
    (installed / toolchains.TRANSACTION).mkdir()
    monkeypatch.setattr(
        toolchains,
        "language_status",
        lambda *a: pytest.fail("must reject before changes"),
    )
    with pytest.raises(ValueError, match="recover"):
        toolchains.install_missing(installed, ["go"])


def test_cli_dispatch_does_not_read_model_configuration(monkeypatch):
    from cli import main

    seen = []
    monkeypatch.setattr(
        "cli.startup.configured_environment",
        lambda *a: pytest.fail("model config is irrelevant"),
    )
    monkeypatch.setattr(toolchains, "main", lambda argv: seen.append(argv))
    monkeypatch.setattr(sys, "argv", ["repo-agent", "toolchains", "list"])
    main.main()
    assert seen == [["list"]]


def test_check_without_brew_is_informational_and_never_prompts(installed, monkeypatch):
    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr(setup, "native_preflight", lambda: [])
    monkeypatch.setattr(setup, "preparation_report", lambda *a: [("ERROR", "Homebrew", "missing")])
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--check cannot prompt"))
    monkeypatch.setattr(sys, "argv", ["setup", "--agent-home", str(installed), "--check"])
    setup.main()
    assert (installed / ".venv/bin/python").read_text() == "old python"


def test_multiple_languages_retain_completed_work_on_later_failure(installed, monkeypatch):
    monkeypatch.setattr(toolchains, "native_preflight", lambda: [])
    monkeypatch.setattr(toolchains, "language_status", lambda root: states(["python"]))

    def install(root, name):
        if name == "go":
            raise subprocess.CalledProcessError(1, ["go", "install"])

    monkeypatch.setattr(toolchains, "install_server", install)
    with pytest.raises(subprocess.CalledProcessError):
        toolchains.install_missing(installed, ["typescript", "go"])
    assert load_record(installed)["languages"] == ["python", "typescript"]
