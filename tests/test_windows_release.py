"""Windows release packaging and installation ownership/rollback contracts."""

import importlib.util
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support import integration, paths, windows_install
from installer import install_transaction, installation, setup, uninstall

ROOT = Path(__file__).resolve().parents[1]


def test_windows_bootstrap_is_the_only_top_level_entry():
    import build_manifest

    files = {
        p.relative_to(ROOT).as_posix()
        for p in build_manifest.bootstrap_files(ROOT, target="windows-x86_64")
    }
    assert "install_release.ps1" in files
    assert not any(name.endswith(".sh") for name in files)
    assert not {"install.ps1", "uninstall.ps1"} & files
    posix = {p.name for p in build_manifest.bootstrap_files(ROOT, target="linux-x86_64")}
    assert "install-release.sh" in posix and "install_release.ps1" not in posix


def test_runtime_lock_windows_matches_platform_and_sha():
    from host_support.platforms import release_target

    records = [
        line.split()
        for line in (ROOT / "runtime/python.lock").read_text().splitlines()
        if line.startswith("windows-x86_64 ")
    ]
    assert len(records) == 1
    target, version, digest, url = records[0]
    assert version == "3.13.15" and len(digest) == 64
    assert "x86_64-pc-windows-msvc-install_only_stripped.tar.gz" in url
    release = release_target(target)
    assert release.archive_suffix == ".zip"
    assert release.runtime_archive_suffix == ".tar.gz"
    assert release.runtime_python == "python/python.exe"
    assert release.wheel_platforms() == ["win_amd64"]


def test_cross_build_evaluates_markers_for_windows(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "prepare", ROOT / "scripts/prepare_python_bundle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source, target = tmp_path / "input.lock", tmp_path / "output.lock"
    source.write_text(
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n    --hash=sha256:abc\n"
        "linux-only==1 ; os_name == 'posix' --hash=sha256:def\n"
        "shared==2 ; python_full_version < '3.15' --hash=sha256:ghi\n"
    )
    module.windows_requirements(source, target, "3.13.15")
    output = target.read_text()
    assert "colorama==0.4.6" in output and "shared==2" in output
    assert "linux-only" not in output and ";" not in output
    assert "--hash=sha256:abc" in output


@pytest.mark.parametrize("name", ["../escape", "C:stream", "file.", "NUL", "a\\b", "a\0b", "a\\b/"])
def test_zip_paths_rejected_before_writing(tmp_path, name):
    from installer.paths import extract_files

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("safe", b"safe")
        output.writestr("x" * len(name), b"bad")
    # Patch both local and central headers without ZipInfo sanitizing the name.
    archive.write_bytes(archive.read_bytes().replace(b"x" * len(name), name.encode("ascii")))
    with zipfile.ZipFile(archive) as source:
        assert source.infolist()[1].orig_filename == name
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError):
        extract_files(archive, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize(
    "names",
    [
        ("file", "file/child"),
        ("folder/file", "folder/file/child"),
        ("Folder/a", "folder/b"),
    ],
)
def test_zip_ambiguous_tree_rejected_before_writing(tmp_path, names):
    from installer.paths import extract_files

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in names:
            output.writestr(name, b"content")
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(ValueError):
        extract_files(archive, destination)
    assert not list(destination.iterdir())


def test_windows_defaults_local_and_preserves_recorded_mode(tmp_path, monkeypatch):
    from installer import dependencies

    monkeypatch.setattr(dependencies, "sys", SimpleNamespace(platform="win32"))
    assert dependencies.available_mode(tmp_path) == "local"
    (tmp_path / ".repo-agent-install.json").write_text(
        json.dumps(
            {
                "root": str(tmp_path.resolve()),
                "mode": "docker",
            }
        ),
        encoding="utf-8",
    )
    assert dependencies.available_mode(tmp_path) == "docker"


def test_runtime_bytecode_filter_preserves_sources_and_rejects_sourceless(tmp_path):
    from scripts.prepare_python_bundle import strip_runtime_bytecode

    source = tmp_path / "module.py"
    source.write_bytes(b"answer = 42\n")
    cache = tmp_path / "__pycache__/module.cpython-313.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"regenerable")
    legacy = tmp_path / "module.pyc"
    legacy.write_bytes(b"regenerable")
    orphan = tmp_path / "orphan.pyc"
    orphan.write_bytes(b"only implementation")
    with pytest.raises(ValueError, match="sourceless"):
        strip_runtime_bytecode(tmp_path)
    assert cache.exists() and legacy.exists() and orphan.exists()
    orphan.unlink()
    strip_runtime_bytecode(tmp_path)
    assert source.read_bytes() == b"answer = 42\n"
    assert not list(tmp_path.rglob("*.pyc"))


def test_zip_raw_backslash_rejected_under_windows_normalization(tmp_path, monkeypatch):
    # Exercise ZipInfo's Windows normalization without changing pathlib's OS.
    monkeypatch.setattr(zipfile, "os", SimpleNamespace(**(vars(os) | {"sep": "\\", "altsep": "/"})))
    test_zip_paths_rejected_before_writing(tmp_path, "a\\b")


def test_zip_forward_slash_remains_valid(tmp_path):
    from installer.paths import extract_files

    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("folder/", b"")
        output.writestr("folder/file.txt", b"content")
    extract_files(archive, tmp_path / "out")
    assert (tmp_path / "out/folder/file.txt").read_bytes() == b"content"


@pytest.fixture
def windows_host(tmp_path, monkeypatch):
    # Exercise Windows integration without monkeypatching the process-global
    # os.name (which would make pathlib use WindowsPath on POSIX).
    if os.name != "nt":
        proxy = SimpleNamespace(**{**vars(os), "name": "nt"})
        for module in (paths, integration, setup, installation, install_transaction, uninstall):
            monkeypatch.setattr(module, "os", proxy)
        monkeypatch.setattr(paths, "sys", SimpleNamespace(platform="win32"))
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.delenv("AGENT_CONFIG_DIR", raising=False)
    registry = {"value": "existing-path", "type": 2}
    monkeypatch.setattr(windows_install, "read_user_path", lambda: dict(registry))
    monkeypatch.setattr(windows_install, "write_user_path", lambda value: registry.update(value))
    return registry


def create_install(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    (root / ".env.example").write_text("LLM_MODEL=\n", encoding="utf-8")
    record = installation.begin_install(root)
    installation.prepare_venv(record)
    (root / ".venv/pyvenv.cfg").write_text("home = managed-python\n")
    for command in installation.COMMANDS:
        target = paths.installed_command(root, command)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ" + name.encode() + command.encode())
    return root, record


def test_windows_command_takeover_requires_confirmation_and_preserves_external_edits(
    tmp_path,
    monkeypatch,
    windows_host,
):
    first, record = create_install(tmp_path, "first")
    second, _ = create_install(tmp_path, "second")
    bins = tmp_path / "commands"
    for name in installation.COMMANDS:
        setup.install_command(first, bins, name, record)
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(ValueError, match="取消"):
        setup.confirm_commands(second, bins)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    states = setup.confirm_commands(second, bins)
    command = setup.install_command(second, bins, approved_states=states)
    assert integration.command_state(command) == str(paths.installed_command(second, "repo-agent"))
    assert not uninstall.unlink_owned_command(record["commands"][0], first, dry_run=False)
    command.write_bytes(b"MZexternal")
    with pytest.raises(ValueError, match="归属"):
        integration.command_state(command)


def test_windows_install_uninstall_preserves_configuration_and_shared_state(tmp_path, windows_host):
    root, record = create_install(tmp_path, "中文 install")
    bins = tmp_path / "commands"
    for name in installation.COMMANDS:
        setup.install_command(root, bins, name, record)
    config = setup.configure_user(root, record)
    config.write_text("LLM_MODEL=my-model\n", encoding="utf-8")
    setup.configure_path(bins, record)
    record["status"] = "installed"
    installation.save_record(record)
    assert installation.load_record(root)["commands"] == record["commands"]
    assert uninstall.uninstall(root, dry_run=True)
    assert paths.public_command(bins, "repo-agent").is_file()
    assert uninstall.uninstall(root)
    assert not (root / ".venv").exists()
    assert not list(bins.glob("*.exe*"))
    assert config.read_text(encoding="utf-8") == "LLM_MODEL=my-model\n"
    assert windows_host["value"] == "existing-path"
    assert uninstall.uninstall(root, purge=True)
    assert not config.exists()


def test_windows_partial_launcher_publication_rolls_back(tmp_path, monkeypatch, windows_host):
    root, record = create_install(tmp_path, "install")
    bins = tmp_path / "bin"
    for name in installation.COMMANDS:
        setup.install_command(root, bins, name, record)
    before = {p: p.read_bytes() for p in bins.iterdir()}
    states = setup.confirm_commands(root, bins)
    transaction = install_transaction.InstallTransaction(root, bins, states)
    target = paths.installed_command(root, "repo-agent")
    target.write_bytes(b"MZnew-version")
    transaction.change_link("repo-agent")
    original = windows_install.atomic_write

    def fail_receipt(path, *args, **kwargs):
        if path.name.endswith(".json"):
            raise OSError("injected receipt failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(windows_install, "atomic_write", fail_receipt)
    with pytest.raises(OSError):
        setup.install_command(root, bins, record=record, approved_states=states)
    install_transaction.recover_install(root)
    assert {p: p.read_bytes() for p in bins.iterdir()} == before
    assert not transaction.directory.exists()


def test_windows_path_rollback_refuses_unrelated_changes(tmp_path, windows_host):
    change = windows_install.path_change(tmp_path / "bin")
    windows_install.validate_path_change(change)
    windows_install.apply_path_change(change)
    windows_host["value"] += ";external"
    with pytest.raises(ValueError, match="其他程序"):
        windows_install.apply_path_change(change, restore=True)
    assert windows_host["value"].endswith(";external")


def test_windows_path_rollback_after_interrupted_install(tmp_path, windows_host):
    root, record = create_install(tmp_path, "install")
    bins = tmp_path / "bin"
    states = {name: None for name in installation.COMMANDS}
    transaction = install_transaction.InstallTransaction(root, bins, states)
    setup.configure_path(bins, record, transaction)
    assert windows_host["value"] != "existing-path"
    install_transaction.recover_install(root)
    assert windows_host["value"] == "existing-path"


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("REPO_AGENT_WINDOWS_ARCHIVE"),
    reason="Requires built Windows ZIP and a real Windows kernel",
)
def test_real_windows_release_survives_download_removal_and_uninstalls(tmp_path):
    from installer.paths import extract_files
    from installer.release_install import locate_release_root
    from installer.release_manifest import read_release

    archive = Path(os.environ["REPO_AGENT_WINDOWS_ARCHIVE"]).resolve()
    tmp_path = tmp_path / "中文 release"
    tmp_path.mkdir()
    download = tmp_path / "download"
    download.mkdir()
    extract_files(archive, download)
    bundle = locate_release_root(download)
    manifest = read_release(bundle)
    runtime_files = {
        name.removeprefix("runtime/"): expected
        for name, expected in manifest["files"].items()
        if name.startswith("runtime/python/")
    }
    assert runtime_files and not any(name.endswith(".pyc") for name in runtime_files)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AGENT_", "LLM_", "XDG_", "PYTHON")) and not key.endswith("_API_KEY")
    }
    env.update(
        PYTHONUTF8="1",
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        AGENT_PYTHON_CACHE=str(tmp_path / "runtime"),
    )
    bins = tmp_path / "commands with spaces"
    powershell = shutil.which("powershell.exe")

    def run(args):
        result = subprocess.run(
            [str(arg) for arg in args], env=env, capture_output=True, timeout=300
        )
        assert result.returncode == 0, result.stdout.decode(
            errors="replace"
        ) + result.stderr.decode(errors="replace")
        return result.stdout.decode("utf-8")

    def script(root, *arguments):
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            root / "install_release.ps1",
            *arguments,
        ]

    run(script(bundle, "--check", "--mode", "local", "--bin-dir", bins))
    assert not (tmp_path / "data").exists() and not (tmp_path / "runtime").exists()
    record = next(
        line.split()
        for line in (bundle / "runtime/python.lock").read_text().splitlines()
        if line.startswith("windows-x86_64 ")
    )
    legacy = tmp_path / "runtime" / f"{record[1]}-windows-x86_64-{record[2][:12]}"
    old_cache = legacy / "python/Lib/__pycache__/__future__.cpython-313.pyc"
    old_cache.parent.mkdir(parents=True)
    old_cache.write_bytes(b"old runtime must remain available for rollback")
    run(script(bundle, "--mode", "local", "--offline", "--no-path", "--bin-dir", bins))
    assert old_cache.read_bytes() == b"old runtime must remain available for rollback"
    root = tmp_path / "data/repo-agent/versions" / manifest["version"]

    def verify_runtime_sources():
        from installer.release_manifest import digest

        caches = [p.parent for p in (tmp_path / "runtime").glob("*/.verified")]
        assert len(caches) == 1
        for base in [root / "runtime", *caches]:
            for name, expected in runtime_files.items():
                assert digest(base / name) == expected, str(base / name)
        return caches[0]

    cache = verify_runtime_sources()
    config = tmp_path / "config/repo-agent/.env"
    config.write_text("LLM_MODEL=keep\n", encoding="utf-8")
    shutil.rmtree(download)
    info = json.loads(run([bins / "repo-agent.exe", "version"]))
    assert info["installation"] == str(root) and info["kind"] == "release"
    verify_runtime_sources()
    # An unlisted bytecode file must be removed before a later bootstrap runs.
    stale = cache / "python/Lib/__pycache__/untrusted.cpython-313.pyc"
    stale.parent.mkdir(exist_ok=True)
    stale.write_bytes(b"unverified old cache")
    run(script(root, "--offline", "--no-path", "--bin-dir", bins))
    assert not stale.exists()
    verify_runtime_sources()
    assert config.read_text(encoding="utf-8") == "LLM_MODEL=keep\n"
    run(script(root, "--recover"))
    run(script(root, "--uninstall", "--dry-run"))
    assert (root / ".venv").is_dir()
    run(script(root, "--uninstall"))
    assert not (root / ".venv").exists() and config.is_file()
    assert not (bins / "repo-agent.exe").exists()
    run(script(root, "--uninstall", "--purge"))
    assert not config.exists()
    source = root / "runtime/python/Lib/__future__.py"
    source.write_bytes(source.read_bytes() + b"\n# changed\n")
    rejected = subprocess.run(
        [str(arg) for arg in script(root, "--check")], env=env, capture_output=True, timeout=30
    )
    assert rejected.returncode != 0
    assert b"SHA256 mismatch" in rejected.stderr
