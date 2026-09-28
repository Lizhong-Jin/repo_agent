"""Deletion boundaries, interrupted installation recovery, and shared resources."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from installer import install_network
from installer.installation import (
    COMMANDS,
    MANIFEST,
    begin_install,
    load_record,
    prepare_venv,
    save_record,
)
from installer.setup import configure_path, configure_user, install_command
from installer.uninstall import uninstall

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def download_executor_for_installer_mocks(monkeypatch):
    monkeypatch.setattr(
        install_network, "execute", lambda command, **kw: subprocess.run(command, **kw)
    )


@pytest.fixture
def installations(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "AGENT_CONFIG_DIR", "ZDOTDIR"):
        monkeypatch.delenv(key, raising=False)

    def create(name="agent", *, custom_bin=False, shell=False):
        root = tmp_path / name
        root.mkdir()
        (root / ".env.example").write_text("LLM_MODEL=\nDEEPSEEK_API_KEY=\n")
        (root / "source.py").write_text("# user's work\n")
        data = begin_install(root)
        prepare_venv(data)
        (root / ".venv/pyvenv.cfg").write_text("home = /python\n")
        (root / ".venv/bin").mkdir()
        config = configure_user(root, data)
        if not config.read_text().strip().endswith("test-secret"):
            config.write_text("DEEPSEEK_API_KEY=test-secret\n")
        bin_dir = home / (f"{name}-bin" if custom_bin else ".local/bin")
        for name in ("repo-agent", "repo-agent-build-sandbox"):
            (root / ".venv/bin" / name).write_text("entry")
            # Another installation may already own a shared public name.
            command = bin_dir / name
            if not command.exists() and not command.is_symlink():
                install_command(root, bin_dir, name, data)
        if shell:
            configure_path(bin_dir, data)
        data["status"] = "installed"
        save_record(data)
        return root, data, bin_dir

    return create


def snapshot(directory):
    return {
        str(path.relative_to(directory)): (
            ("link", os.readlink(path)) if path.is_symlink() else ("file", path.read_bytes())
        )
        for path in directory.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def test_dry_run_then_uninstall_then_purge(installations, tmp_path, capsys):
    root, data, bins = installations(shell=True)
    (root / "logs").mkdir()
    (root / "logs/session.log").write_text("saved work")
    (root / ".env").write_text("project config")
    before = snapshot(tmp_path)
    assert uninstall(root, dry_run=True, purge=True)
    assert snapshot(tmp_path) == before
    assert uninstall(root)
    assert not (root / ".venv").exists()
    assert not (root / MANIFEST).exists()
    assert not (bins / "repo-agent").is_symlink()
    assert Path(data["config"]).exists()
    assert (root / "logs/session.log").read_text() == "saved work"
    assert (root / ".env").read_text() == "project config"
    assert (root / "source.py").read_text() == "# user's work\n"
    assert (Path.home() / ".zshrc").exists()  # Shared ~/.local/bin remains useful.
    assert load_record(root)["status"] == "uninstalled"
    assert uninstall(root)
    assert uninstall(root, purge=True)
    assert not Path(data["config"]).exists()
    assert "test-secret" not in capsys.readouterr().out
    assert "test-secret" not in Path(data["registry"]).read_text()


def test_shared_config_and_redirected_command_are_retained(installations):
    first, first_data, bins = installations()
    second, _, _ = installations("second")
    command = bins / "repo-agent"
    command.unlink()
    command.symlink_to(second / ".venv/bin/repo-agent")
    assert uninstall(first, purge=True)
    assert command.resolve() == second / ".venv/bin/repo-agent"
    assert (second / ".venv").exists()
    assert Path(first_data["config"]).exists()


def test_registry_alone_protects_shared_configuration(installations):
    first, data, _ = installations(custom_bin=True)
    second, _, _ = installations("second", custom_bin=True)
    assert uninstall(first, purge=True)
    assert Path(data["config"]).exists()
    assert uninstall(second, purge=True)
    assert not Path(data["config"]).exists()


@pytest.mark.parametrize("modified,other_tool", [(False, False), (True, False), (False, True)])
def test_only_unchanged_unshared_shell_blocks_are_removed(installations, modified, other_tool):
    rc = Path.home() / ".zshrc"
    rc.write_text("# user settings\n")
    root, _, bins = installations(custom_bin=True, shell=True)
    if modified:
        rc.write_text(rc.read_text().replace("export PATH=", "export PATH=/extra:"))
    if other_tool:
        (bins / "another-tool").write_text("another tool")
    before = rc.read_text()
    assert uninstall(root)
    assert rc.read_text() == (before if modified or other_tool else "# user settings\n")


def test_copied_record_cannot_uninstall_its_origin(installations, tmp_path):
    original, original_data, _ = installations()
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="其他目录"):
        uninstall(copied, purge=True)
    assert snapshot(tmp_path) == before
    copied_data = begin_install(copied)
    prepare_venv(copied_data)
    assert copied_data["id"] != original_data["id"]
    assert uninstall(copied)
    assert (original / ".venv").exists()


@pytest.mark.parametrize("change", ["symlink", "foreign_marker", "missing_marker"])
def test_venv_ownership_is_rechecked(installations, tmp_path, change):
    root, _, _ = installations()
    marker = root / ".venv" / MANIFEST
    if change == "symlink":
        external = tmp_path / "external-env"
        (root / ".venv").rename(external)
        (root / ".venv").symlink_to(external, target_is_directory=True)
    elif change == "foreign_marker":
        marker.write_text('{"id":"foreign"}')
    else:
        marker.unlink()
    assert uninstall(root) is False
    assert (root / ".venv").exists()


def test_unrecorded_installation_is_not_guessed(installations, tmp_path):
    root = tmp_path / "unknown"
    (root / ".venv").mkdir(parents=True)
    (root / ".venv/anything").write_text("keep")
    before = snapshot(tmp_path)
    assert uninstall(root, purge=True, remove_image=True)
    assert snapshot(tmp_path) == before


def test_invalid_registry_preserves_configuration(installations):
    root, data, _ = installations()
    (Path(data["registry"]).parent / "broken.json").write_text("invalid")
    assert uninstall(root, purge=True)
    assert Path(data["config"]).exists()


def test_changed_config_symlink_is_not_followed(installations, tmp_path):
    root, data, _ = installations()
    config = Path(data["config"])
    outside = tmp_path / "other-config"
    outside.write_text("keep")
    config.unlink()
    config.symlink_to(outside)
    assert uninstall(root, purge=True)
    assert outside.read_text() == "keep"
    assert config.is_symlink()


@pytest.mark.parametrize(
    "mode", ["delete", "dry_run", "changed_image", "changed_daemon", "in_use", "shared", "offline"]
)
def test_image_cleanup_checks_ownership_and_never_forces(installations, monkeypatch, mode):
    root, data, _ = installations(custom_bin=True)
    data["images"] = [{"tag": "repo-agent-sandbox:v1", "id": "sha256:original", "daemon": "daemon"}]
    save_record(data)
    if mode == "shared":
        installations("second", custom_bin=True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert "--force" not in command and "-f" not in command
        if mode == "offline":
            raise FileNotFoundError("docker")
        if command[1] == "info":
            value = "other-daemon" if mode == "changed_daemon" else "daemon"
        elif command[1:3] == ["image", "inspect"]:
            value = "sha256:new" if mode == "changed_image" else "sha256:original"
        elif command[1] == "ps":
            value = "container" if mode == "in_use" else ""
        else:
            assert command == ["docker", "image", "rm", "repo-agent-sandbox:v1"]
            value = ""
        return SimpleNamespace(returncode=0, stdout=value)

    monkeypatch.setattr("installer.uninstall.subprocess.run", run)
    result = uninstall(root, remove_image=True, dry_run=mode == "dry_run")
    assert result is (mode != "offline")
    deleted = [command for command in calls if command[1:3] == ["image", "rm"]]
    assert bool(deleted) is (mode == "delete")
    if mode == "dry_run":
        assert (root / ".venv").exists()
    else:
        assert not (root / ".venv").exists()


@pytest.mark.parametrize("takeover", [False, True])
def test_invalid_install_is_rejected_before_creating_environment(installations, tmp_path, takeover):
    if takeover:
        old, _, bins = installations(custom_bin=True)
    root = tmp_path / "failed-install"
    root.mkdir(parents=True)
    for package in ("cli", "installer", "configuration", "host_support"):
        shutil.copytree(SOURCE / package, root / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("install.sh", "scripts/installer-entry.sh", "uninstall.sh"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, root / name)
    # Missing project metadata must fail preflight, before creating an environment.
    env = {
        **os.environ,
        "AGENT_PYTHON": sys.executable,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    result = subprocess.run(
        [str(root / "install.sh"), "--mode", "local", "--skip-sandbox"]
        + (["--bin-dir", str(bins)] if takeover else []),
        env=env,
        input="y\n",
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode != 0
    assert "缺少 pyproject.toml" in result.stdout
    assert not (root / MANIFEST).exists() and not (root / ".venv").exists()
    if takeover:
        assert load_record(root) is None
        for name in COMMANDS:
            assert (bins / name).resolve() == old / ".venv/bin" / name
    result = subprocess.run(
        [sys.executable, "-S", str(root / "installer/uninstall.py"), "--agent-home", str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".venv").exists()
    assert (root / "install.sh").exists()
    if takeover:
        assert (old / ".venv").exists()
        for name in COMMANDS:
            assert (bins / name).resolve() == old / ".venv/bin" / name
    result = subprocess.run(
        [str(root / "uninstall.sh")], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unregistered_copy_blocks_purge_after_command_conflict(installations, tmp_path):
    root, data, bins = installations()
    # Simulate a failed install before it managed to register the existing public commands.
    data["commands"] = []
    save_record(data)
    foreign = tmp_path / "unregistered copy/.venv/bin/repo-agent"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("other installation")
    (bins / "repo-agent").unlink()
    (bins / "repo-agent").symlink_to(foreign)
    assert uninstall(root, purge=True)
    assert Path(data["config"]).exists()
    assert (bins / "repo-agent").resolve() == foreign


def test_reinstall_preserves_identity_and_can_prepare_partial_venv_again(installations, tmp_path):
    root = tmp_path / "partial"
    root.mkdir()
    data = begin_install(root)
    prepare_venv(data)
    again = begin_install(root)
    prepare_venv(again)  # No pyvenv.cfg yet: the owned partial environment is still recognized.
    assert again["id"] == data["id"]
    assert uninstall(root)


def test_malformed_journal_fails_before_deleting_any_command(installations):
    from installer.installation import write_json

    root, data, bins = installations()
    data["commands"].append({"path": "/unexpected", "target": "/unexpected"})
    write_json(root / MANIFEST, data)
    with pytest.raises(ValueError):
        uninstall(root)
    assert (bins / "repo-agent").is_symlink()
    assert (root / ".venv").exists()


def test_successful_rebuild_records_image_and_preserves_installation_fields(
    installations, monkeypatch
):
    from installer.installation import record_image

    root, data, _ = installations()
    replies = iter(["sha256:new-image", "my-daemon"])
    monkeypatch.setattr(
        "installer.installation.subprocess.run",
        lambda *a, **kw: SimpleNamespace(stdout=next(replies), returncode=0),
    )
    record_image(root, "custom:test")
    updated = load_record(root)
    assert updated["commands"] == data["commands"]
    assert updated["images"] == [
        {"tag": "custom:test", "id": "sha256:new-image", "daemon": "my-daemon"}
    ]


@pytest.mark.parametrize("uninstall_old_first", [False, True])
def test_confirmed_takeover_and_uninstall_do_not_restore_old_commands(
    installations, tmp_path, monkeypatch, uninstall_old_first
):
    from installer import setup

    old, _, bins = installations()
    new, _, _ = installations("new install")
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return "y"

    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup", "--agent-home", str(new), "--mode", "local", "--skip-sandbox", "--no-path"],
    )
    setup.main()
    assert len(prompts) == 1
    assert {Path(item["path"]).name for item in load_record(new)["commands"]} == set(COMMANDS)
    for name in COMMANDS:
        assert (bins / name).resolve() == new / ".venv/bin" / name
    # Same-directory reinstall never asks again.
    setup.main()
    assert len(prompts) == 1
    if uninstall_old_first:
        assert uninstall(old)
        for name in COMMANDS:
            assert (bins / name).resolve() == new / ".venv/bin" / name
    assert uninstall(new)
    for name in COMMANDS:
        assert not (bins / name).is_symlink()
    if not uninstall_old_first:
        assert (old / ".venv").exists()


@pytest.mark.parametrize("answer", ["", "\n", "n\n"])
def test_shell_install_cancellation_precedes_all_writes(installations, tmp_path, answer):
    old, _, bins = installations()
    new = tmp_path / "not installed"
    new.mkdir(parents=True)
    for package in ("cli", "installer", "configuration", "host_support"):
        shutil.copytree(SOURCE / package, new / package,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("install.sh", "scripts/installer-entry.sh", "uninstall.sh"):
        (new / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, new / name)
    before = snapshot(tmp_path)
    result = subprocess.run(
        [str(new / "install.sh"), "--mode", "local", "--skip-sandbox"],
        env={**os.environ, "AGENT_PYTHON": sys.executable, "PYTHONDONTWRITEBYTECODE": "1"},
        input=answer,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "已取消安装" in result.stderr
    assert str(old) in result.stdout and str(new) in result.stdout
    assert snapshot(tmp_path) == before
    assert not (new / ".venv").exists()
    for name in COMMANDS:
        assert (bins / name).resolve() == old / ".venv/bin" / name


def test_bootstrap_confirms_once_before_creating_environment(installations, tmp_path, monkeypatch):
    from installer import setup

    old, _, bins = installations()
    new = tmp_path / "fresh"
    new.mkdir()
    (new / ".env.example").write_text("LLM_MODEL=\n")
    events = []

    def confirm(prompt):
        assert not (new / MANIFEST).exists()
        assert not (new / ".venv").exists()
        events.append("confirm")
        return "yes"

    def run(command, **kwargs):
        for name in COMMANDS:
            assert (bins / name).resolve() == old / ".venv/bin" / name
        if command[1:3] == ["-m", "venv"]:
            events.append("venv")
        else:
            if command[1:3] != ["-m", "pip"] or command[3] == "check":
                events.append("smoke")
                return
            if "--require-hashes" in command:
                assert str(new / "requirements-dev.lock") in command
                events.append("locked")
                return
            assert command[:5] == [str(new / ".venv/bin/python"), "-m", "pip", "install", "-e"]
            events.append("pip")
            (new / ".venv/bin").mkdir()
            for name in COMMANDS:
                (new / ".venv/bin" / name).write_text("entry")

    monkeypatch.setattr(setup, "print_language_status", lambda root: None)
    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr("builtins.input", confirm)
    monkeypatch.setattr(setup.subprocess, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup",
            "--bootstrap",
            "--agent-home",
            str(new),
            "--mode",
            "local",
            "--skip-sandbox",
            "--no-path",
        ],
    )
    setup.main()
    assert events == ["confirm", "venv", "locked", "pip", "smoke", "smoke", "smoke", "smoke"]
    for name in COMMANDS:
        assert (bins / name).resolve() == new / ".venv/bin" / name


@pytest.mark.parametrize("failure", ["build", "changed_link", "missing_entry"])
def test_takeover_failure_preserves_old_commands(installations, monkeypatch, failure):
    from installer import setup

    old, _, bins = installations()
    new, _, _ = installations("new")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    def build(command, **kwargs):
        if failure == "build":
            raise subprocess.CalledProcessError(1, command)
        if failure == "missing_entry":
            (new / ".venv/bin/repo-agent-build-sandbox").unlink()
        else:
            (bins / "repo-agent-build-sandbox").unlink()
            (bins / "repo-agent-build-sandbox").write_text("another tool")

    monkeypatch.setattr(setup.subprocess, "run", build)
    monkeypatch.setattr(setup, "record_image", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["setup", "--agent-home", str(new), "--mode", "docker"])
    with pytest.raises(SystemExit) as error:
        setup.main()
    assert error.value.code == 1
    assert (bins / "repo-agent").resolve() == old / ".venv/bin/repo-agent"
    if failure != "changed_link":
        assert (
            bins / "repo-agent-build-sandbox"
        ).resolve() == old / ".venv/bin/repo-agent-build-sandbox"
    else:
        assert (bins / "repo-agent-build-sandbox").read_text() == "another tool"


@pytest.mark.parametrize("kind", ["file", "directory", "unrelated_link"])
def test_unrelated_command_blocks_takeover_before_prompt(installations, monkeypatch, kind):
    from installer.setup import confirm_commands

    _, _, bins = installations()
    new, _, _ = installations("new")
    command = bins / "repo-agent-build-sandbox"
    command.unlink()
    if kind == "file":
        command.write_text("keep")
    elif kind == "directory":
        command.mkdir()
    else:
        command.symlink_to("/unrelated/program")
    monkeypatch.setattr("builtins.input", lambda *args: pytest.fail("must reject before prompting"))
    before = snapshot(bins)
    with pytest.raises(ValueError, match="保留已有命令"):
        confirm_commands(new, bins)
    assert snapshot(bins) == before


def test_dangling_relative_install_links_can_be_replaced(installations, monkeypatch):
    from installer import setup

    _, _, bins = installations()
    new, _, _ = installations("new")
    for name in COMMANDS:
        (bins / name).unlink()
        (bins / name).symlink_to(f"../../deleted/.venv/bin/{name}")
    monkeypatch.setattr("builtins.input", lambda prompt: " Y ")
    monkeypatch.setattr(
        sys,
        "argv",
        ["setup", "--agent-home", str(new), "--mode", "local", "--skip-sandbox", "--no-path"],
    )
    setup.main()
    for name in COMMANDS:
        assert (bins / name).resolve() == new / ".venv/bin" / name


def test_uninstall_keeps_preferences_until_unshared_purge(installations):
    root, data, _ = installations()
    preferences = Path(data["config"]).with_name("thinking.json")
    preferences.write_text('{"version":1,"models":{}}')
    assert uninstall(root, dry_run=True, purge=True)
    assert preferences.exists()
    assert uninstall(root)
    assert preferences.exists()
    assert uninstall(root, purge=True)
    assert not preferences.exists()


@pytest.mark.parametrize("from_download", [False, True])
def test_release_entry_uninstalls_with_broken_venv_and_stdlib_python(
    installations, tmp_path, from_download
):
    import json
    import shlex

    import build_manifest
    from installer.release_manifest import digest

    (tmp_path / "data/versions").mkdir(parents=True)
    root, data, bins = installations("data/versions/0.1.0")
    names = build_manifest.bootstrap_files(SOURCE)
    build_manifest.copy_files(SOURCE, root, names)
    names = [path.relative_to(SOURCE).as_posix() for path in names]
    wheel = "wheels/repo_agent-0.1.0-py3-none-any.whl"
    (root / "wheels").mkdir()
    (root / wheel).write_bytes(b"placeholder")
    manifest = {
        "schema": 4,
        "target": "macos-arm64",
        "name": "repo-agent",
        "version": "0.1.0",
        "wheel": wheel,
        "files": {name: digest(root / name) for name in [*names, wheel]},
    }
    (root / "release.json").write_text(json.dumps(manifest))
    entry = root
    if from_download:
        entry = tmp_path / "download"
        build_manifest.copy_files(root, entry, [root / name for name in [*names, wheel, "release.json"]])
    # Damaged release resources and venv must not block journal-based cleanup.
    (root / ".env.example").unlink()
    python = root / ".venv/bin/python"
    python.write_text("#!/bin/bash\nexit 1\n")
    python.chmod(0o755)
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    fallback = system_bin / "python3"
    fallback.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable) + ' -S "$@"\n')
    fallback.chmod(0o755)
    # Maintenance may inspect cached Python but must never download it.
    (entry / "scripts/bootstrap-python.sh").write_text(
        '#!/bin/bash\n[[ "$2" == --recover ]] || touch "$1/unexpected-download"\nexit 1\n'
    )
    env = {key: value for key, value in os.environ.items() if key != "AGENT_PYTHON"}
    env["PATH"] = str(system_bin) + os.pathsep + env.get("PATH", "")
    args = ["/bin/bash", str(entry / "install-release.sh"), "--uninstall"]
    if from_download:
        args += ["--data-dir", str(tmp_path / "data")]
    before = snapshot(tmp_path)
    preview = subprocess.run(args + ["--dry-run"], env=env, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert snapshot(tmp_path) == before
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / ".venv").exists()
    assert not (bins / "repo-agent").is_symlink()
    assert Path(data["config"]).exists()
    assert (root / "install-release.sh").exists()
    assert not (entry / "unexpected-download").exists()
