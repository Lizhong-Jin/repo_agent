"""Interpreter-specific repair guidance; no package manager is executed."""

import shlex
from types import SimpleNamespace

import pytest

from installer import maintenance


def interpreter(monkeypatch, prefix, *, platform="linux", executable=None):
    executable = executable or str(prefix) + "/bin/python3.12"
    monkeypatch.setattr(
        maintenance,
        "sys",
        SimpleNamespace(
            platform=platform,
            base_prefix=str(prefix),
            executable=executable,
            _base_executable=executable,
            version_info=(3, 12, 7),
            version="3.12.7",
        ),
    )


def test_debian_matches_selected_python_not_default_python(monkeypatch):
    interpreter(monkeypatch, "/usr", executable="/usr/bin/python3.12")
    monkeypatch.setattr(
        maintenance.platform,
        "freedesktop_os_release",
        lambda: {"ID": "ubuntu", "ID_LIKE": "debian"},
    )
    text = maintenance.python_component_guidance()
    assert "sudo apt-get install python3.12-venv" in text
    assert "AGENT_PYTHON=/usr/bin/python3.12 ./install.sh --check" in text
    assert "--mode" in text


def test_conda_repairs_base_environment_even_inside_venv(tmp_path, monkeypatch):
    prefix = tmp_path / "Conda Install"
    (prefix / "conda-meta").mkdir(parents=True)
    interpreter(monkeypatch, prefix, executable="/task/.venv/bin/python")
    maintenance.sys._base_executable = str(prefix / "bin/python3.12")
    monkeypatch.setattr(
        maintenance.platform,
        "freedesktop_os_release",
        lambda: pytest.fail("Conda takes precedence"),
    )
    text = maintenance.python_component_guidance()
    command = next(line.strip() for line in text.splitlines() if "conda install --prefix" in line)
    assert shlex.split(command) == [
        "conda",
        "install",
        "--prefix",
        str(prefix),
        "--force-reinstall",
        "python=3.12",
    ]
    retry = next(line.strip() for line in text.splitlines() if "./install.sh --check" in line)
    assert shlex.split(retry)[0] == "AGENT_PYTHON=" + str(prefix / "bin/python3.12")
    assert "apt-get" not in text


@pytest.mark.parametrize(
    "prefix,formula",
    [
        (
            "/opt/homebrew/Cellar/python@3.12/3.12.7/Frameworks/Python.framework/Versions/3.12",
            "python@3.12",
        ),
        ("/home/linuxbrew/.linuxbrew/opt/python/lib", "python"),
    ],
)
def test_brew_uses_correct_formula_and_stable_path(monkeypatch, prefix, formula):
    interpreter(monkeypatch, prefix)
    text = maintenance.python_component_guidance()
    assert f"brew reinstall {formula}" in text
    assert f'AGENT_PYTHON="$(brew --prefix {formula})/libexec/bin/python3"' in text
    assert "apt-get" not in text


def test_apple_python_can_switch_to_full_python(monkeypatch):
    interpreter(
        monkeypatch,
        "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.12",
        platform="darwin",
    )
    text = maintenance.python_component_guidance()
    assert "brew install python" in text
    assert "https://www.python.org/downloads/macos/" in text
    assert "brew reinstall" not in text


def test_custom_linux_python_does_not_suggest_apt_for_wrong_interpreter(monkeypatch):
    interpreter(monkeypatch, "/home/test/.pyenv/versions/3.12.7")
    monkeypatch.setattr(
        maintenance.platform,
        "freedesktop_os_release",
        lambda: pytest.fail("custom Python must not use apt"),
    )
    text = maintenance.python_component_guidance()
    assert "原安装方式" in text
    assert "apt-get" not in text


@pytest.mark.parametrize(
    "release,expected",
    [
        (
            {"ID": "rocky", "ID_LIKE": "rhel centos fedora"},
            "dnf provides '*/python3.12/venv/__init__.py'",
        ),
        ({"ID": "arch"}, "sudo pacman -S python"),
        ({"ID": "unknown"}, "本发行版的软件包管理器"),
    ],
)
def test_other_linux_distributions(monkeypatch, release, expected):
    interpreter(monkeypatch, "/usr")
    monkeypatch.setattr(maintenance.platform, "freedesktop_os_release", lambda: release)
    assert expected in maintenance.python_component_guidance()


def test_missing_os_release_keeps_original_diagnostic(monkeypatch):
    interpreter(monkeypatch, "/usr")

    def missing():
        raise OSError("no os-release")

    monkeypatch.setattr(maintenance.platform, "freedesktop_os_release", missing)
    assert "本发行版的软件包管理器" in maintenance.python_component_guidance()


@pytest.mark.parametrize("missing", [("venv",), ("ensurepip",), ("venv", "ensurepip")])
def test_exact_missing_components_are_reported_without_running_commands(monkeypatch, missing):
    def module(name):
        if name in missing:
            raise ImportError(name)
        return SimpleNamespace(EnvBuilder=lambda: None, version=lambda: "24.0")

    monkeypatch.setattr(maintenance.importlib, "import_module", module)
    monkeypatch.setattr(maintenance, "python_component_guidance", lambda: "repair instructions")
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda *a, **kw: pytest.fail("guidance must not execute commands"),
    )
    rows = maintenance.python_components_report()
    assert rows == [
        ("ERROR", "安装组件", "缺少或不可用：" + ", ".join(missing) + "\nrepair instructions")
    ]


def test_healthy_components_do_not_show_repairs(monkeypatch):
    monkeypatch.setattr(
        maintenance, "python_component_guidance", lambda: pytest.fail("no repair needed")
    )
    assert maintenance.python_components_report() == [("OK", "安装组件", "venv 和 ensurepip 可用")]


def test_environment_check_surfaces_repair_as_error_without_writes(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / ".env.example").write_text("LLM_MODEL=\n")
    original = maintenance.importlib.import_module

    def module(name):
        if name == "ensurepip":
            raise ImportError("ensurepip")
        return original(name)

    monkeypatch.setattr(maintenance.importlib, "import_module", module)
    monkeypatch.setattr(maintenance, "python_component_guidance", lambda: "specific repair command")
    monkeypatch.setattr(
        maintenance.subprocess, "run", lambda *a, **kw: pytest.fail("check must stay read-only")
    )
    before = sorted(tmp_path.iterdir())
    rows = maintenance.environment_report(tmp_path, docker=False)
    assert ("ERROR", "安装组件", "缺少或不可用：ensurepip\nspecific repair command") in rows
    assert not maintenance.print_report(rows)
    assert sorted(tmp_path.iterdir()) == before
