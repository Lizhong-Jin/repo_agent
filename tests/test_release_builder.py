"""Complete platform payloads, versioned outputs and offline build selection."""

import importlib.util
import json
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

import build_manifest
from installer.release_manifest import digest, read_release

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def builder(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location(
        'release_builder', ROOT / 'scripts/build_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def build_inputs(builder, tmp_path, monkeypatch):
    root = tmp_path / 'source'
    root.mkdir()
    build_manifest.copy_files(ROOT, root, build_manifest.source_files(ROOT))
    build_manifest.copy_files(ROOT, root, build_manifest.bootstrap_files(ROOT))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if 'build' in command:
            output = Path(command[command.index('--outdir') + 1])
            output.mkdir()
            version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
            with zipfile.ZipFile(output / f'repo_agent-{version}-py3-none-any.whl', 'w') as wheel:
                for source, destination in builder.RESOURCE_FILES:
                    wheel.write(root / source, destination)
                wheel.writestr(builder.CONTEXT_ARCHIVE, b'context')

    def prepare(root, output, target, **kwargs):
        (output / 'runtime').mkdir(parents=True)
        (output / 'runtime/target').write_text(target + '\n')
        if target == 'windows-x86_64':
            (output / 'runtime/python').mkdir()
            (output / 'runtime/python/python.exe').write_bytes(target.encode())
        else:
            (output / 'runtime/python.tar.gz').write_bytes(target.encode())
        (output / 'wheelhouse').mkdir()
        (output / f'wheelhouse/dependency-{target}.whl').write_bytes(target.encode())
        (output / 'bundle.json').write_text(json.dumps({'target': target}))

    monkeypatch.setattr(builder.subprocess, 'run', run)
    monkeypatch.setattr(builder, 'export_locks', lambda *a, **kw: None)
    monkeypatch.setattr(builder, 'verify_wheel', lambda *a: None)
    monkeypatch.setattr(builder, 'prepare', prepare)
    return root, tmp_path / 'dist', calls


def test_default_builds_all_complete_platforms_with_one_shared_wheel(
    builder, build_inputs, tmp_path,
):
    root, output, calls = build_inputs
    archives = builder.build(root, output, 'uv')
    targets = list(builder.runtime_records(root))
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    assert len(archives) == len(targets) == 5
    assert len([command for command in calls if 'build' in command]) == 1
    wheels = set()
    for target, archive in zip(targets, archives, strict=True):
        prefix = f'repo-agent-{version}-{target}'
        suffix = '.zip' if target == 'windows-x86_64' else '.tar.gz'
        assert archive == output / version / target / f'{prefix}{suffix}'
        extracted = tmp_path / f'extracted-{target}'
        from installer.paths import extract_files
        extracted.mkdir()
        extract_files(archive, extracted)
        bundle = extracted / prefix
        manifest = read_release(bundle)
        assert manifest["schema"] == 4 and manifest["target"] == target
        assert "installer/setup.py" in manifest["files"]
        assert "cli/setup.py" not in manifest["files"]
        assert (bundle / 'runtime/target').read_text().strip() == target
        runtime = 'runtime/python/python.exe' if target == 'windows-x86_64' else 'runtime/python.tar.gz'
        assert (bundle / runtime).read_bytes() == target.encode()
        entries = {p.name for p in bundle.iterdir() if p.suffix in {'.sh', '.ps1'}}
        assert entries == ({'install_release.ps1'} if target == 'windows-x86_64' else {'install-release.sh'})
        assert list((bundle / 'wheelhouse').iterdir()) == [
            bundle / f'wheelhouse/dependency-{target}.whl']
        wheels.add(digest(bundle / manifest['wheel']))
        assert archive.with_name(archive.name + '.sha256').read_text() == (
            f'{digest(archive)}  {archive.name}\n')
    assert len(wheels) == 1
    assert sorted(output.iterdir()) == [output / version]
    assert not list(output.rglob('*.whl'))  # Wheel is an internal package component.


def test_single_target_and_new_version_preserve_other_artifacts(builder, build_inputs):
    root, output, _ = build_inputs
    first = builder.build(root, output, 'uv', target='macos-arm64')[0]
    before = first.read_bytes()
    metadata = root / 'pyproject.toml'
    old = tomllib.loads(metadata.read_text())['project']['version']
    metadata.write_text(metadata.read_text().replace(f'version = "{old}"', 'version = "9.8.7"'))
    second = builder.build(root, output, 'uv', target='linux-arm64')[0]
    assert second.parent == output / '9.8.7/linux-arm64'
    assert first.read_bytes() == before
    assert sorted(path.name for path in (output / '9.8.7').iterdir()) == ['linux-arm64']


def test_failed_platform_prepare_keeps_existing_release(builder, build_inputs, monkeypatch):
    root, output, _ = build_inputs
    archive = builder.build(root, output, 'uv', target='macos-arm64')[0]
    before = archive.read_bytes()

    def fail(*args, **kwargs):
        raise ValueError('Python SHA256 mismatch')

    monkeypatch.setattr(builder, 'prepare', fail)
    with pytest.raises(ValueError, match='SHA256'):
        builder.build(root, output, 'uv', target='macos-arm64')
    assert archive.read_bytes() == before
    assert not list(output.rglob('.release-*'))


def test_all_platform_offline_archives_and_missing_input_fail_early(builder, tmp_path):
    targets = list(builder.runtime_records())
    runtime = tmp_path / 'runtime-archives'
    runtime.mkdir()
    for target in targets:
        (runtime / f'{target}.tar.gz').touch()
    assert builder.runtime_archives(targets, runtime, offline=True) == {
        target: runtime / f'{target}.tar.gz' for target in targets}
    (runtime / f'{targets[-1]}.tar.gz').unlink()
    with pytest.raises(ValueError, match='缺少平台'):
        builder.runtime_archives(targets, runtime, offline=True)
    with pytest.raises(ValueError, match='必须指定 --target'):
        builder.runtime_archives(targets, runtime / f'{targets[0]}.tar.gz', offline=False)
    with pytest.raises(ValueError, match='离线构建需要'):
        builder.runtime_archives(targets, None, offline=True)


@pytest.mark.parametrize('target', ['', 'windows-arm64'])
def test_unsupported_or_empty_target_cannot_build_lightweight_package(
    builder, build_inputs, target,
):
    root, output, calls = build_inputs
    with pytest.raises(ValueError, match='不支持'):
        builder.build(root, output, 'uv', target=target)
    assert not calls and not output.exists()


def test_cli_without_target_uses_all_platform_default(builder, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(builder, 'build', lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(sys, 'argv', ['build_release.py', '--uv', 'uv', '--output', str(tmp_path)])
    builder.main()
    assert calls[0][0][1] == tmp_path
    assert calls[0][1]['target'] is None


def test_incomplete_installer_fails_before_replacing_archive(builder, build_inputs, monkeypatch):
    root, output, _ = build_inputs
    archive = builder.build(root, output, "uv", target="macos-arm64")[0]
    before = archive.read_bytes()
    checksum = archive.with_name(archive.name + ".sha256").read_bytes()
    original = builder.bootstrap_files

    def omit_setup(*args, **kwargs):
        return [
            path
            for path in original(*args, **kwargs)
            if path.relative_to(root).as_posix() != "installer/setup.py"
        ]

    monkeypatch.setattr(builder, "bootstrap_files", omit_setup)
    with pytest.raises(ValueError, match="必要文件"):
        builder.build(root, output, "uv", target="macos-arm64")
    assert archive.read_bytes() == before
    assert archive.with_name(archive.name + ".sha256").read_bytes() == checksum
