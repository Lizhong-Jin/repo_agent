"""Failure injection and recovery contracts. No Docker builds or network requests."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import config_command, doctor
from configuration.environment import CONFIG_KEYS, read_config, save_user_config
from configuration.storage import backup_config, backups, config_lock
from host_support.locking import file_lock
from installer import install_network, setup
from installer.install_transaction import TRANSACTION, InstallTransaction, recover_install
from installer.installation import (
    COMMANDS,
    MANIFEST,
    begin_install,
    load_record,
    prepare_venv,
    save_record,
    user_config_path,
)
from installer.maintenance import environment_report
from installer.uninstall import uninstall

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def download_executor_for_installer_mocks(monkeypatch):
    monkeypatch.setattr(
        install_network, "execute", lambda command, **kw: subprocess.run(command, **kw)
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    for key in CONFIG_KEYS | {
        "AGENT_CONFIG_DIR",
        "AGENT_ENV_FILE",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "ZDOTDIR",
        "PIP_CERT",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    }:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def installed(tmp_path, name="install"):
    root = tmp_path / name
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="test-agent"\nversion="0.0.0"\n')
    (root / ".env.example").write_text("LLM_PROVIDER=deepseek\nLLM_MODEL=\nDEEPSEEK_API_KEY=\n")
    record = begin_install(root)
    prepare_venv(record)
    (root / ".venv/pyvenv.cfg").write_text("home = /old/python\n")
    (root / ".venv/bin").mkdir()
    for name in COMMANDS:
        (root / ".venv/bin" / name).write_text("old entry")
        (root / ".venv/bin" / name).chmod(0o700)
    (root / ".venv/old-data").write_text("keep old dependencies")
    bins = tmp_path / "bin"
    for name in COMMANDS:
        setup.install_command(root, bins, name, record)
    record["status"] = "installed"
    save_record(record)
    return root, bins, record


def test_environment_detects_broken_copy_and_bad_ca(tmp_path, monkeypatch):
    root, _, _ = installed(tmp_path)
    monkeypatch.setenv("PIP_CERT", str(tmp_path / "missing-ca.pem"))
    rows = environment_report(root, docker=False)
    assert any(level == "WARN" and label == "虚拟环境" for level, label, _ in rows)
    assert any(level == "ERROR" and "PIP_CERT" in detail for level, _, detail in rows)
    assert (root / ".venv/old-data").read_text() == "keep old dependencies"


def test_environment_rejects_unknown_or_symlink_venv(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    venv = root / ".venv"
    venv.mkdir()
    (venv / "unrelated").write_text("keep")
    assert any(
        level == "ERROR" and label == "虚拟环境"
        for level, label, _ in environment_report(root, docker=False)
    )
    shutil.rmtree(venv)
    venv.symlink_to(tmp_path / "missing")
    assert any(
        level == "ERROR" and label == "虚拟环境"
        for level, label, _ in environment_report(root, docker=False)
    )


@pytest.mark.parametrize("failure", ["venv", "pip", "smoke", "second-link", "shell", "interrupt"])
def test_reinstall_failure_restores_venv_links_records_and_shell(tmp_path, monkeypatch, failure):
    root, bins, record = installed(tmp_path)
    original = (root / MANIFEST).read_bytes()
    original_inode = (root / ".venv").stat().st_ino
    rc = Path.home() / ".zshrc"
    rc.write_text("# existing shell settings\n")
    config = user_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("LLM_MODEL=mine\nDEEPSEEK_API_KEY=keep-secret\n")
    monkeypatch.setattr(setup, "environment_report", lambda *a, **kw: [])

    def run(command, **kwargs):
        stage = (
            "venv"
            if command[1:3] == ["-m", "venv"]
            else "pip"
            if command[1:4] == ["-m", "pip", "install"]
            else "smoke"
        )
        if failure == stage:
            raise subprocess.CalledProcessError(1, command)
        if failure == "interrupt" and stage == "pip":
            raise KeyboardInterrupt
        if stage == "pip":
            (root / ".venv/bin").mkdir()
            for name in COMMANDS:
                (root / ".venv/bin" / name).write_text("new entry")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup.subprocess, "run", run)
    original_link = setup.install_command

    def link(*args, **kwargs):
        if failure == "second-link" and args[2] == COMMANDS[1]:
            raise OSError("simulated second link failure")
        return original_link(*args, **kwargs)

    monkeypatch.setattr(setup, "install_command", link)
    original_shell = setup.configure_path

    def shell(*args, **kwargs):
        result = original_shell(*args, **kwargs)
        if failure == "shell":
            raise OSError("simulated shell failure")
        return result

    monkeypatch.setattr(setup, "configure_path", shell)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup",
            "--agent-home",
            str(root),
            "--bin-dir",
            str(bins),
            "--bootstrap",
            "--mode",
            "local",
            "--skip-sandbox",
        ],
    )
    with pytest.raises(SystemExit) as error:
        setup.main()
    assert error.value.code == 1
    assert (root / ".venv").stat().st_ino == original_inode
    assert (root / ".venv/old-data").read_text() == "keep old dependencies"
    assert (root / MANIFEST).read_bytes() == original
    assert Path(record["registry"]).read_bytes() == original
    for name in COMMANDS:
        assert (bins / name).resolve() == root / ".venv/bin" / name
    assert rc.read_text() == "# existing shell settings\n"
    assert config.read_text() == "LLM_MODEL=mine\nDEEPSEEK_API_KEY=keep-secret\n"
    assert not (root / TRANSACTION).exists()


def test_failed_takeover_restores_both_old_links(tmp_path, monkeypatch):
    old, bins, _ = installed(tmp_path, "old")
    new = tmp_path / "new"
    (new / ".venv/bin").mkdir(parents=True)
    (new / ".env.example").write_text("LLM_MODEL=\n")
    for name in COMMANDS:
        (new / ".venv/bin" / name).write_text("new")
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    original = setup.install_command

    def link(*args, **kwargs):
        if args[2] == COMMANDS[1]:
            raise OSError("failure after first link")
        return original(*args, **kwargs)

    monkeypatch.setattr(setup, "install_command", link)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "setup",
            "--agent-home",
            str(new),
            "--bin-dir",
            str(bins),
            "--mode",
            "local",
            "--skip-sandbox",
        ],
    )
    with pytest.raises(SystemExit):
        setup.main()
    assert all((bins / name).resolve() == old / ".venv/bin" / name for name in COMMANDS)
    assert load_record(new) is None


def test_durable_recovery_after_process_exit(tmp_path):
    root, bins, _ = installed(tmp_path)
    original = (root / MANIFEST).read_bytes()
    script = (
        "from pathlib import Path; import os; "
        "from installer.install_transaction import InstallTransaction; "
        "from installer.setup import command_state; "
        "from installer.installation import COMMANDS,begin_install; "
        "r=Path(os.environ['TEST_ROOT']); b=Path(os.environ['TEST_BIN']); "
        "t=InstallTransaction(r,b,{n:command_state(b/n) for n in COMMANDS}); "
        "begin_install(r); t.fresh_venv(); "
        "(r/'.venv/partial').write_text('partial'); os._exit(9)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "PYTHONPATH": str(SOURCE),
            "TEST_ROOT": str(root),
            "TEST_BIN": str(bins),
        },
        timeout=15,
    )
    assert result.returncode == 9
    assert (root / TRANSACTION).exists()
    recover_install(root)
    assert (root / MANIFEST).read_bytes() == original
    assert (root / ".venv/old-data").exists()
    assert not (root / ".venv/partial").exists()
    recover_install(root)  # Repeat recovery is harmless.


def test_recovery_refuses_copied_transaction(tmp_path):
    root, bins, _ = installed(tmp_path)
    InstallTransaction(root, bins, {name: os.readlink(bins / name) for name in COMMANDS})
    copied = tmp_path / "copy"
    shutil.copytree(root, copied)
    with pytest.raises(ValueError, match="其他目录"):
        recover_install(copied)
    assert (root / TRANSACTION).exists()
    recover_install(root)


def test_rollback_keeps_externally_changed_command_and_retains_recovery(tmp_path):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.change_link(COMMANDS[0])
    (bins / COMMANDS[0]).unlink()
    (bins / COMMANDS[0]).write_text("external tool")
    with pytest.raises(ValueError, match="恢复未完成"):
        transaction.rollback()
    assert (bins / COMMANDS[0]).read_text() == "external tool"
    assert (root / TRANSACTION).exists()


def test_success_commit_discards_backup_but_keeps_new_environment(tmp_path):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.fresh_venv()
    (root / ".venv/new-data").write_text("new")
    transaction.commit()
    assert (root / ".venv/new-data").exists()
    assert not (root / TRANSACTION).exists()


def test_lock_prevents_parallel_install_and_config(tmp_path):
    root, bins, _ = installed(tmp_path)
    with file_lock(root / ".repo-agent-operation.lock"):
        with pytest.raises(ValueError, match="另一个"):
            uninstall(root)
    with config_lock(user_config_path()):
        with pytest.raises(ValueError, match="另一个"):
            save_user_config({"LLM_MODEL": "racing"})
    assert not user_config_path().exists()


@pytest.mark.parametrize("existing", [False, True])
def test_actual_offline_pip_failure_rolls_back(tmp_path, existing):
    if existing:
        root, bins, _ = installed(tmp_path)
    else:
        root, bins = tmp_path / "fresh", tmp_path / "bin"
        root.mkdir()
    for package in ("cli", "installer", "configuration", "host_support"):
        shutil.copytree(
            SOURCE / package, root / package, ignore=shutil.ignore_patterns("__pycache__")
        )
    shutil.copy2(SOURCE / "install.sh", root / "install.sh")
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copy2(SOURCE / "scripts/installer-entry.sh", root / "scripts/installer-entry.sh")
    (root / ".env.example").write_text("LLM_MODEL=\n")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires=["repo-agent-test-unavailable-build-package==0.0.0"]\nbuild-backend="unavailable"\n'
    )
    result = subprocess.run(
        [str(root / "install.sh"), "--mode", "local", "--skip-sandbox", "--bin-dir", str(bins)],
        env={
            **os.environ,
            "AGENT_PYTHON": sys.executable,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 1
    assert "已恢复安装前" in result.stdout
    assert not (root / TRANSACTION).exists()
    if existing:
        assert (root / ".venv/old-data").exists()
        assert load_record(root)["status"] == "installed"
    else:
        assert not (root / ".venv").exists()
        assert load_record(root) is None


def test_config_backups_restore_broken_file_and_unset_preserves_comments(capsys):
    save_user_config({"LLM_MODEL": "first", "DEEPSEEK_API_KEY": "keep-secret"})
    config_command.main(["backup"])
    first = backups(user_config_path())[0]
    save_user_config({"LLM_MODEL": "second"})
    path = user_config_path()
    path.write_text("broken configuration with private-secret")
    config_command.main(["restore", first.name])
    assert read_config(path)["LLM_MODEL"] == "first"
    assert any(p.read_text() == "broken configuration with private-secret" for p in backups(path))
    config_command.main(["unset", "LLM_MODEL"])
    assert "LLM_MODEL" not in read_config(path)
    assert read_config(path)["DEEPSEEK_API_KEY"] == "keep-secret"
    config_command.main(["backups"])
    output = capsys.readouterr()
    assert "keep-secret" not in output.out + output.err
    assert "private-secret" not in output.out + output.err
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in backups(path))
    assert first.parent.stat().st_mode & 0o777 == 0o700


def test_config_reset_recovers_malformed_file_and_preserves_backup(capsys):
    save_user_config({"LLM_MODEL": "before"})
    path = user_config_path()
    path.write_bytes(b"bad syntax and secret")
    config_command.main(["reset"])
    assert path.read_bytes() == (SOURCE / ".env.example").read_bytes()
    assert any(p.read_bytes() == b"bad syntax and secret" for p in backups(path))
    assert "secret" not in capsys.readouterr().out


def test_invalid_backup_or_path_traversal_never_overwrites_config(capsys):
    save_user_config({"LLM_MODEL": "before"})
    path = user_config_path()
    path.write_text("broken secret")
    broken = backup_config(path)
    path.write_text("LLM_MODEL=current\n")
    for name in (broken.name, "../../elsewhere"):
        with pytest.raises(SystemExit) as error:
            config_command.main(["restore", name])
        assert error.value.code == 1
        assert read_config(path)["LLM_MODEL"] == "current"
    assert "secret" not in capsys.readouterr().err


def test_validation_detects_conflicts_and_unknown_fields_without_requests(capsys):
    save_user_config({"LLM_THINKING": "disabled", "LLM_THINKING_BUDGET": "1024"})
    with pytest.raises(SystemExit) as error:
        config_command.main(["validate", "--user"])
    assert error.value.code == 1
    config_command.main(["unset", "LLM_THINKING_BUDGET"])
    path = user_config_path()
    path.write_text(path.read_text() + "UNRECOGNIZED=hidden-secret\nLLM_MODEL=\nLLM_MODEL=\n")
    config_command.main(["validate"])
    output = capsys.readouterr()
    assert "未知配置项" in output.out and "重复配置项" in output.out
    assert "hidden-secret" not in output.out + output.err
    assert "未设置" in output.out


def test_purge_removes_private_backups_but_preserves_unrelated_files(tmp_path):
    root, _, _ = installed(tmp_path)
    save_user_config({"LLM_MODEL": "one"})
    save_user_config({"LLM_MODEL": "two"})
    directory = backups(user_config_path())[0].parent
    (directory / "unrelated.txt").write_text("keep")
    assert uninstall(root, purge=True)
    assert not user_config_path().exists()
    assert backups(user_config_path()) == []
    assert (directory / "unrelated.txt").read_text() == "keep"


def test_doctor_reports_broken_config_path_shadowing_and_no_secrets(tmp_path, monkeypatch, capsys):
    root, bins, _ = installed(tmp_path)
    monkeypatch.setenv("PATH", str(bins))
    save_user_config({"DEEPSEEK_API_KEY": "private-key", "LLM_STREAM": "invalid"})
    monkeypatch.setattr(doctor, "environment_report", lambda *a, **kw: [])
    monkeypatch.setattr(doctor, "probe", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=""))
    rows = doctor.diagnose(root, tmp_path, docker=False)
    assert any(level == "ERROR" and label == "配置" for level, label, _ in rows)
    assert any(level == "OK" and label == "repo-agent" for level, label, _ in rows)
    assert "private-key" not in str(rows)
    assert not (tmp_path / "logs").exists()


def test_doctor_dispatch_bypasses_broken_configuration(monkeypatch):
    from cli import main

    monkeypatch.setenv("LLM_STREAM", "invalid")
    monkeypatch.setattr(sys, "argv", ["repo-agent", "doctor", "--skip-docker"])
    seen = []
    monkeypatch.setattr(doctor, "main", lambda args: seen.append(args))
    main.main()
    assert seen == [["--skip-docker"]]


@pytest.mark.parametrize("had_old", [False, True])
def test_image_promotion_failure_restores_previous_tag(tmp_path, monkeypatch, had_old):
    from installer.installation import DEFAULT_IMAGE

    root, bins, _ = installed(tmp_path)
    images = {DEFAULT_IMAGE: "sha256:aaa"} if had_old else {}

    def docker_run(command, **kwargs):
        if command[1] == "info":
            return SimpleNamespace(returncode=0, stdout="daemon-1\n")
        if command[1:3] == ["image", "inspect"]:
            value = images.get(command[-1])
            return SimpleNamespace(returncode=0 if value else 1, stdout=value or "")
        if command[1] == "tag":
            images[command[-1]] = images.get(command[-2], command[-2])
        elif command[1:3] == ["image", "rm"]:
            images.pop(command[-1], None)
        else:
            pytest.fail(f"Unexpected command: {command}")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("installer.install_transaction.subprocess.run", docker_run)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    tag = transaction.stage_image()
    images[tag] = "sha256:bbb"
    transaction.promote_image()
    assert images[DEFAULT_IMAGE] == "sha256:bbb"
    transaction.rollback()
    assert images == ({DEFAULT_IMAGE: "sha256:aaa"} if had_old else {})


def test_recovery_retry_after_docker_failure_keeps_restored_venv(tmp_path, monkeypatch):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.fresh_venv()
    transaction.state["image"] = {"old": "sha256:aaa", "new": "sha256:bbb", "daemon": "test-daemon"}
    transaction.save()

    def fail(*args, **kwargs):
        raise OSError("Docker unavailable")

    monkeypatch.setattr("installer.install_transaction.subprocess.run", fail)
    with pytest.raises(ValueError, match="恢复未完成"):
        transaction.rollback()
    assert (root / ".venv/old-data").exists()
    monkeypatch.setattr(
        "installer.install_transaction.subprocess.run",
        lambda command, **kw: SimpleNamespace(
            returncode=0, stdout="test-daemon" if command[1] == "info" else "sha256:aaa"
        ),
    )
    recover_install(root)
    assert (root / ".venv/old-data").exists()
    assert not (root / TRANSACTION).exists()


def test_doctor_and_install_check_are_read_only(tmp_path):
    copy = tmp_path / "source"
    for package in ("cli", "installer", "configuration", "host_support"):
        shutil.copytree(
            SOURCE / package, copy / package, ignore=shutil.ignore_patterns("__pycache__")
        )
    for name in ("install.sh", "scripts/installer-entry.sh", "pyproject.toml", ".env.example"):
        (copy / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, copy / name)
    (copy / ".venv/bin").mkdir(parents=True)
    (copy / ".venv/pyvenv.cfg").write_text("home = /old/python\n")
    (copy / ".venv/bin/python").symlink_to(sys.executable)
    for name in COMMANDS:
        (copy / ".venv/bin" / name).symlink_to(SOURCE / ".venv/bin" / name)
    env = {
        **os.environ,
        "PYTHONPATH": str(SOURCE),
        "PYTHONDONTWRITEBYTECODE": "1",
        "AGENT_PYTHON": sys.executable,
    }
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for command in (
        [str(copy / "install.sh"), "--check", "--mode", "local", "--skip-sandbox"],
        [sys.executable, "-B", str(copy / "cli/doctor.py"), "--skip-docker"],
    ):
        result = subprocess.run(
            command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30
        )
        assert result.returncode in {0, 1}, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        assert "TLS 证书" in result.stdout
    after = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_doctor_fallback_runs_without_site_packages(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", "-S", str(SOURCE / "cli/doctor.py"), "--skip-docker"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "Traceback" not in result.stderr
    assert "缺少依赖" in result.stdout
    assert "用户配置" in result.stdout


def test_recovery_rejects_invalid_resource_paths_before_changes(tmp_path):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.state["changed_links"] = ["../../outside"]
    transaction.save()
    with pytest.raises(ValueError, match="恢复记录损坏"):
        recover_install(root)
    assert (root / ".venv/old-data").exists()


def test_failed_commit_record_write_can_still_roll_back(tmp_path, monkeypatch):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.fresh_venv()
    original_save = transaction.save

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(transaction, "save", fail)
    with pytest.raises(OSError):
        transaction.commit()
    assert not transaction.state["committed"]
    monkeypatch.setattr(transaction, "save", original_save)
    transaction.rollback()
    assert (root / ".venv/old-data").exists()


def test_partial_shell_append_is_recovered_without_losing_existing_text(tmp_path):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    rc = Path.home() / ".zshrc"
    before = b"# existing shell settings\n"
    rc.write_bytes(before)
    block = (
        "\n# >>> Repo Agent " + "a" * 32 + "\nexport PATH=test\n# <<< Repo Agent " + "a" * 32 + "\n"
    )
    transaction.append_shell(rc, block)
    rc.write_bytes(before + block[:20].encode())
    recover_install(root)
    assert rc.read_bytes() == before


def test_failed_template_publish_never_leaves_partial_user_config(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env.example").write_text("LLM_MODEL=\n")

    def fail(*args):
        raise OSError("cannot publish")

    monkeypatch.setattr(setup.os, "link", fail)
    with pytest.raises(OSError):
        setup.configure_user(root)
    assert not user_config_path().exists()
    assert list(user_config_path().parent.glob(".config-template-*")) == []


def test_committed_cleanup_failure_remains_recoverable(tmp_path, monkeypatch):
    root, bins, _ = installed(tmp_path)
    transaction = InstallTransaction(
        root, bins, {name: os.readlink(bins / name) for name in COMMANDS}
    )
    transaction.fresh_venv()
    original = shutil.rmtree

    def fail(path, *args, **kwargs):
        if Path(path) == transaction.directory / "venv":
            raise OSError("temporarily busy")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail)
    transaction.commit()
    assert (transaction.directory / "state.json").exists()
    monkeypatch.setattr(shutil, "rmtree", original)
    recover_install(root)
    assert not transaction.directory.exists()
    assert not (root / ".venv/old-data").exists()


def test_changed_registry_location_does_not_mutate_old_record(tmp_path, monkeypatch):
    root, bins, record = installed(tmp_path)
    before = Path(record["registry"]).read_bytes()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "new-state"))
    with pytest.raises(ValueError, match="XDG_STATE_HOME"):
        InstallTransaction(root, bins, {name: os.readlink(bins / name) for name in COMMANDS})
    assert Path(record["registry"]).read_bytes() == before
    assert not (root / TRANSACTION).exists()
