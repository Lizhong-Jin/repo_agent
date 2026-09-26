"""Distribution policy and offline builds of actual wheels/source archives."""

import importlib.util
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

import build_manifest as manifest
from cli.paths import extract_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    manifest.copy_files(ROOT, root, manifest.source_files(ROOT))
    manifest.copy_files(ROOT, root, manifest.bootstrap_files(ROOT))
    return root


def test_manifest_import_and_check_need_only_standard_library():
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "build_manifest.py")],
        cwd="/",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "name",
    [
        ".env.example",
        "requirements-core.lock",
        "build_manifest.py",
        "dependencies/node/package-lock.json",
        "sandbox/Dockerfile",
        "cli/__init__.py",
    ],
)
def test_missing_required_inputs_fail_early(source, name):
    (source / name).unlink()
    with pytest.raises(ValueError, match="missing"):
        manifest.source_files(source)


@pytest.mark.parametrize("name", ["MANIFEST.in", ".dockerignore", "pyproject.toml"])
def test_generated_config_drift_is_rejected_and_repaired(source, name):
    path = source / name
    if name == "pyproject.toml":
        path.write_text(
            path.read_text().replace("include-package-data = false", "include-package-data = true")
        )
    else:
        path.write_text("outdated settings\n")
    with pytest.raises(ValueError, match="out of sync"):
        manifest.check_configuration(source)
    manifest.check_configuration(source, write=True)
    manifest.check_configuration(source)


def test_shared_policy_excludes_local_state_and_refuses_symlinks(source, tmp_path):
    for name in (
        ".env",
        "cli/.env",
        "cli/.venv/secret.py",
        "agent/node_modules/secret.py",
        "tools/logs/private.py",
        "cli/unlisted.json",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-for-distribution")
    selected = {path.relative_to(source).as_posix() for path in manifest.source_files(source)}
    assert ".env.example" in selected
    assert not any(
        "secret" in name or "private" in name or name.endswith("unlisted.json") for name in selected
    )
    outside = tmp_path / "outside.py"
    outside.write_text("must not be copied")
    (source / "cli/linked.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        manifest.source_files(source)


def test_missing_installer_fails_bootstrap_but_is_not_a_wheel_input(source):
    (source / "install.sh").unlink()
    assert manifest.source_files(source)
    with pytest.raises(ValueError, match="install.sh"):
        manifest.bootstrap_files(source)


def build(root, kind):
    # Use the test interpreter's build backend; no network or installation in user directories.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"from setuptools.build_meta import build_{kind}; build_{kind}('dist')",
        ],
        cwd=root,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    suffix = "*.whl" if kind == "wheel" else "*.tar.gz"
    return next((root / "dist").glob(suffix))


def load_policy(root):
    spec = importlib.util.spec_from_file_location(
        "fixture_build_manifest", root / "build_manifest.py"
    )
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


def test_real_wheel_and_sdist_keep_new_resource_and_drop_removed_module(source, tmp_path):
    resource = source / "agent/release_fixture.json"
    resource.write_text('{"fixture": true}')
    policy_file = source / "build_manifest.py"
    policy_file.write_text(
        policy_file.read_text().replace(
            "PACKAGE_RESOURCES = (", 'PACKAGE_RESOURCES = ("agent/release_fixture.json", '
        )
    )
    policy = load_policy(source)
    policy.check_configuration(source, write=True)
    old_module = source / "cli/removed_fixture.py"
    old_module.write_text("old = True\n")
    (source / "cli/.env").write_text("secret")
    (source / "cli/.venv").mkdir()
    (source / "cli/.venv/private.py").write_text("secret")
    (source / "cli/undeclared.json").write_text("secret")
    (source / "cli/.private.py").write_text("secret")
    policy.check_configuration(source, write=True)
    wheel = build(source, "wheel")
    policy.verify_wheel(wheel, source)
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("agent/release_fixture.json") == resource.read_bytes()
        assert "cli/removed_fixture.py" in archive.namelist()
        assert "cli/.env" not in archive.namelist()
        assert "cli/undeclared.json" not in archive.namelist()
    # Rebuild in the SAME tree to exercise stale build/lib and egg-info caches.
    old_module.unlink()
    policy.check_configuration(source, write=True)
    wheel = build(source, "wheel")
    policy.verify_wheel(wheel, source)
    with zipfile.ZipFile(wheel) as archive:
        assert "cli/removed_fixture.py" not in archive.namelist()
    source_archive = build(source, "sdist")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    extract_files(source_archive, extracted)
    rebuilt = next(path for path in extracted.iterdir() if path.is_dir())
    assert (rebuilt / "agent/release_fixture.json").read_bytes() == resource.read_bytes()
    assert (rebuilt / "build_manifest.py").exists()
    assert not (rebuilt / "cli/.env").exists()
    assert not (rebuilt / "cli/.private.py").exists()
    assert not (rebuilt / "cli/.venv/private.py").exists()
    assert not (rebuilt / "cli/removed_fixture.py").exists()
    rebuilt_wheel = build(rebuilt, "wheel")
    policy.verify_wheel(rebuilt_wheel, rebuilt)
    # Check the real built context, not just matching two Python lists.
    with zipfile.ZipFile(rebuilt_wheel) as archive:
        context = tmp_path / "context.tar.gz"
        context.write_bytes(archive.read("cli/resources/docker-context.tar.gz"))
    with tarfile.open(context) as archive:
        assert archive.extractfile("agent/release_fixture.json").read() == resource.read_bytes()
        assert "build_manifest.py" in archive.getnames()
        assert "cli/.env" not in archive.getnames()
    # The release verifier refuses a wheel with an omitted declared resource.
    broken = tmp_path / "broken.whl"
    with zipfile.ZipFile(rebuilt_wheel) as original, zipfile.ZipFile(broken, "w") as output:
        for info in original.infolist():
            if info.filename != "agent/release_fixture.json":
                output.writestr(info, original.read(info.filename))
    with pytest.raises(ValueError, match="missing"):
        policy.verify_wheel(broken, rebuilt)


@pytest.mark.parametrize("name", ["MANIFEST.in", ".dockerignore"])
def test_deleted_generated_file_can_be_recreated(source, name):
    (source / name).unlink()
    manifest.check_configuration(source, write=True)
    manifest.check_configuration(source)
    assert (source / name).is_file()


def test_sdist_rejects_missing_declared_resource(source):
    (source / "requirements-lsp.lock").unlink()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from setuptools.build_meta import build_sdist; build_sdist('dist')",
        ],
        cwd=source,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "requirements-lsp.lock" in result.stderr
    assert not list((source / "dist").glob("*.tar.gz"))


def test_source_docker_entry_rejects_stale_allowlist(source, monkeypatch):
    from cli import paths

    monkeypatch.setattr(paths, "__file__", str(source / "cli/paths.py"))
    (source / "cli/new_module.py").write_text("new = True\n")
    with pytest.raises(ValueError, match="out of sync"):
        with paths.docker_build_context():
            pytest.fail("A stale Docker context must never be used")
    manifest.check_configuration(source, write=True)
    with paths.docker_build_context() as context:
        assert context == source
        assert "!cli/new_module.py" in (context / ".dockerignore").read_text()


def test_release_verification_checks_enclosing_folder(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / ".env.example").write_text("template")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(bundle / ".env.example", arcname="repo-agent-0.1.0/.env.example")
    manifest.verify_release_archive(archive, bundle, prefix="repo-agent-0.1.0")
    with pytest.raises(ValueError):
        manifest.verify_release_archive(archive, bundle, prefix="repo-agent-0.2.0")
