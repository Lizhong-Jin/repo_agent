"""Release boundary checks; optional real archive test uses isolated user directories."""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from cli import paths, release_install
from cli.release_manifest import digest, read_release

SOURCE = Path(__file__).resolve().parents[1]


def bundle_at(root, version="0.1.0"):
    root.mkdir()
    names = [
        "install.sh",
        "install-release.sh",
        "uninstall.sh",
        "cli/setup.py",
        "cli/release_install.py",
        ".env.example",
        "pyproject.toml",
        "requirements-core.lock",
        "requirements-lsp.lock",
        "uv.lock",
        "wheels/repo_agent-0.1.0-py3-none-any.whl",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text(name)
    manifest = {
        "schema": 1,
        "name": "repo-agent",
        "version": version,
        "wheel": names[-1],
        "files": {name: digest(root / name) for name in names},
    }
    (root / "release.json").write_text(json.dumps(manifest))
    return root


def test_release_location_uses_installed_prefix_and_packaged_resources(tmp_path, monkeypatch):
    root = tmp_path / "versions/0.1.0"
    site = root / ".venv/lib/python3.13/site-packages"
    assets = site / "cli/resources"
    assets.mkdir(parents=True)
    (assets / "default.env").write_text("LLM_MODEL=packaged")
    monkeypatch.setattr(paths, "__file__", str(site / "cli/paths.py"))
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("LLM_MODEL=workspace")
    assert paths.installation_root() == root
    assert paths.resource_path("default.env").read_text() == "LLM_MODEL=packaged"
    for name in ("../private", "/private"):
        with pytest.raises(ValueError):
            paths.resource_path(name)


def test_source_resources_ignore_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.installation_root() == SOURCE
    assert paths.resource_path("default.env") == SOURCE / ".env.example"


@pytest.mark.parametrize(
    "names", [["../outside"], ["/absolute"], ["a", "a"], ["a//b", "a/b"], ["link"]]
)
def test_archive_rejects_unsafe_members_before_extracting(tmp_path, names):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        for name in ["safe", *names]:
            info = tarfile.TarInfo(name)
            if name == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "../outside"
            else:
                info.size = 1
            bundle.addfile(info, io.BytesIO(b"x"))
    target = tmp_path / "extract"
    target.mkdir()
    with pytest.raises(ValueError):
        paths.extract_files(archive, target)
    assert not list(target.iterdir())


def test_installed_docker_context_is_complete_and_temporary(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    assets = site / "cli/resources"
    assets.mkdir(parents=True)
    # Wheels may also contain sandbox/Dockerfile; this alone isn't a source checkout.
    (site / "sandbox").mkdir()
    (site / "sandbox/Dockerfile").write_text("not a complete build context")
    with tarfile.open(assets / "docker-context.tar.gz", "w:gz") as bundle:
        info = tarfile.TarInfo("sandbox/Dockerfile")
        info.size = 4
        bundle.addfile(info, io.BytesIO(b"FROM"))
    monkeypatch.setattr(paths, "__file__", str(site / "cli/paths.py"))
    with paths.docker_build_context() as context:
        assert context != site
        assert (context / "sandbox/Dockerfile").read_text() == "FROM"
    assert not context.exists()


def test_release_copy_checks_integrity_and_rejects_version_reuse(tmp_path):
    bundle = bundle_at(tmp_path / "download")
    target = tmp_path / "versions/0.1.0"
    expected = release_install.prepare_release(bundle, target)
    assert read_release(target) == expected
    assert release_install.prepare_release(bundle, target) == expected
    (bundle / ".env.example").write_text("tampered")
    with pytest.raises(ValueError, match="校验失败"):
        release_install.prepare_release(bundle, target)
    assert (target / ".env.example").read_text() == ".env.example"
    expected["files"][".env.example"] = digest(bundle / ".env.example")
    (bundle / "release.json").write_text(json.dumps(expected))
    with pytest.raises(ValueError, match="同版本"):
        release_install.prepare_release(bundle, target)


@pytest.mark.parametrize(
    "value", [[], {"schema": 1, "name": "repo-agent", "version": [], "files": {}}]
)
def test_invalid_manifest_has_actionable_error(tmp_path, value):
    (tmp_path / "release.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="发行清单"):
        read_release(tmp_path)


def test_check_and_cancel_do_not_create_version(tmp_path, monkeypatch):
    bundle = bundle_at(tmp_path / "download")
    monkeypatch.setattr(release_install, "__file__", str(bundle / "cli/release_install.py"))
    data = tmp_path / "data"
    seen = []
    monkeypatch.setattr(release_install.setup, "main", lambda args, **kw: seen.append(args))
    release_install.main(["--data-dir", str(data), "--check", "--mode", "local"])
    assert seen and "--check" in seen[0] and not data.exists()

    def cancel(*args):
        assert not (data / "versions").exists()
        raise ValueError("已取消安装")

    monkeypatch.setattr(release_install.setup, "confirm_commands", cancel)
    with pytest.raises(SystemExit):
        release_install.main(["--data-dir", str(data), "--mode", "local"])
    assert not (data / "versions").exists()


def test_confirm_once_before_staging_and_preserve_config(tmp_path, monkeypatch):
    bundle = bundle_at(tmp_path / "download")
    monkeypatch.setattr(release_install, "__file__", str(bundle / "cli/release_install.py"))
    data = tmp_path / "data"
    events = []
    approved = {"repo-agent": "old-target"}

    def confirm(*args):
        assert not (data / "versions").exists()
        events.append("confirm")
        return approved

    def setup(args, *, approved_commands):
        events.append("setup")
        assert approved_commands is approved
        assert "--bootstrap" in args and "--wheel" in args
        assert read_release(data / "versions/0.1.0")

    monkeypatch.setattr(release_install.setup, "confirm_commands", confirm)
    monkeypatch.setattr(release_install.setup, "main", setup)
    release_install.main(["--data-dir", str(data), "--mode", "local"])
    assert events == ["confirm", "setup"]


def test_node_lock_matches_declared_versions():
    from cli.dependencies import TYPESCRIPT, TYPESCRIPT_SERVER

    directory = SOURCE / "dependencies/node"
    package = json.loads((directory / "package.json").read_text())
    lock = json.loads((directory / "package-lock.json").read_text())
    assert package["dependencies"] == lock["packages"][""]["dependencies"]
    assert package["dependencies"] == {
        "typescript": TYPESCRIPT,
        "typescript-language-server": TYPESCRIPT_SERVER,
    }
    assert all(
        item.get("integrity", "").startswith("sha512-")
        for name, item in lock["packages"].items()
        if name
    )


@pytest.mark.skipif(
    not os.environ.get("REPO_AGENT_TEST_ARCHIVE"),
    reason="requires an explicitly built release archive and network",
)
def test_real_release_survives_download_removal(tmp_path, monkeypatch):
    archive = Path(os.environ["REPO_AGENT_TEST_ARCHIVE"]).resolve()
    bundle = tmp_path / "download"
    bundle.mkdir()
    paths.extract_files(archive, bundle)
    manifest = read_release(bundle)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AGENT_", "LLM_", "XDG_", "PYTHON")) and not key.endswith("_API_KEY")
    }
    env.update(
        HOME=str(tmp_path / "home"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        AGENT_PYTHON=sys.executable,
        PATH=str(tmp_path / "bin") + os.pathsep + env.get("PATH", ""),
    )
    (tmp_path / "home").mkdir()
    workspace = tmp_path / "unrelated project"
    workspace.mkdir()

    def run(args, **kwargs):
        result = subprocess.run(
            [str(item) for item in args],
            env=env,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=300,
            **kwargs,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run(
        [
            "/bin/bash",
            bundle / "install-release.sh",
            "--mode",
            "local",
            "--no-path",
            "--bin-dir",
            tmp_path / "bin",
        ]
    )
    shutil.rmtree(bundle)
    command = tmp_path / "bin/repo-agent"
    info = json.loads(run([command, "version"]))
    root = tmp_path / "data/repo-agent/versions" / manifest["version"]
    assert info["installation"] == str(root) and info["kind"] == "release"
    origin = json.loads(
        run(
            [
                root / ".venv/bin/python",
                "-I",
                "-c",
                "import cli, json; print(json.dumps(cli.__file__))",
            ]
        )
    )
    assert Path(origin).is_relative_to(root / ".venv")
    run([command, "config", "show"])
    run([command, "config", "reset"])
    config = tmp_path / "config/repo-agent/.env"
    assert config.read_bytes() == (SOURCE / ".env.example").read_bytes()
    run([command, "config", "set", "LLM_MODEL", "release-test"])
    config.write_text(
        config.read_text().replace("DEEPSEEK_API_KEY=\n", "DEEPSEEK_API_KEY=fake-key-never-sent\n")
    )
    run([command, "doctor", "--mode", "local"])
    run([command, "toolchains", "list"])
    run([command, "--sandbox", "local"], input="/exit\n")
    trace = json.loads(next((workspace / "logs").glob("*.trace.jsonl")).read_text().splitlines()[0])
    assert trace["workspace"] == str(workspace) and trace["model"] == "release-test"
    # Capture context while it exists; do not require a Docker daemon or model access.
    docker = tmp_path / "bin/docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        + "import json, sys\nfrom pathlib import Path\n"
        + "if sys.argv[1] == 'info':\n print(json.dumps({'OSType':'linux','Architecture':'arm64','Runtimes':{'runc':{}}}))\n"
        + "elif sys.argv[1] == 'build':\n"
        + " root = Path(sys.argv[-1])\n"
        + " for name in ['pyproject.toml','build_support.py','.env.example','.dockerignore','requirements-lsp.lock','dependencies/node/package-lock.json','sandbox/Dockerfile','sandbox/worker.py','tools/factory.py']: assert (root/name).is_file(), name\n"
        + " assert not (root/'.env').exists()\n"
        + "else: sys.exit(99)\n"
    )
    docker.chmod(0o755)
    run([tmp_path / "bin/repo-agent-build-sandbox", "--profile", "standard"])
    before = config.read_bytes()
    run([command, "uninstall", "--dry-run"])
    assert command.exists()
    run([command, "uninstall"])
    assert not command.is_symlink() and not (root / ".venv").exists()
    assert config.read_bytes() == before


def test_recovery_from_custom_version_directory_keeps_location(tmp_path, monkeypatch):
    data = tmp_path / "custom-data"
    (data / "versions").mkdir(parents=True)
    bundle = bundle_at(data / "versions/0.1.0")
    monkeypatch.setattr(release_install, "__file__", str(bundle / "cli/release_install.py"))
    seen = []
    monkeypatch.setattr(release_install.setup, "main", lambda args, **kw: seen.append(args))
    monkeypatch.setattr(
        release_install.setup,
        "confirm_commands",
        lambda *a: pytest.fail("recovery must not prompt"),
    )
    release_install.main(["--recover"])
    assert seen[0][seen[0].index("--agent-home") + 1] == str(bundle)


def test_release_dependency_failure_preserves_old_command_and_config(tmp_path, monkeypatch):
    from cli.installation import COMMANDS

    bundle = bundle_at(tmp_path / "download")
    monkeypatch.setattr(release_install, "__file__", str(bundle / "cli/release_install.py"))
    old = tmp_path / "old/.venv/bin"
    old.mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in COMMANDS:
        (old / name).write_text("old entry")
        (binaries / name).symlink_to(old / name)
    config = tmp_path / "config/.env"
    config.parent.mkdir()
    config.write_text("LLM_MODEL=keep-user-model\n")
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(config.parent))
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    monkeypatch.setattr(release_install.setup, "environment_report", lambda *a, **kw: [])

    def fail(command, **kwargs):
        assert "--require-hashes" in command
        raise ValueError("simulated dependency download failure")

    monkeypatch.setattr(release_install.setup, "run_download", fail)
    with pytest.raises(SystemExit):
        release_install.main(
            [
                "--data-dir",
                str(tmp_path / "data"),
                "--bin-dir",
                str(binaries),
                "--mode",
                "local",
                "--no-path",
            ]
        )
    for name in COMMANDS:
        assert (binaries / name).resolve() == old / name
        assert (old / name).read_text() == "old entry"
    target = tmp_path / "data/versions/0.1.0"
    assert not (target / ".venv").exists()
    assert read_release(target)  # verified payload remains available for a retry
    assert config.read_text() == "LLM_MODEL=keep-user-model\n"
