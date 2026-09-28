"""Prepare a platform-specific Python + wheelhouse kit without running its Python."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from host_support.platforms import PlatformInfo, release_target  # noqa: E402
from installer.paths import extract_files  # noqa: E402


def runtime_records(root=ROOT):
    records = {}
    for line in (root / 'runtime/python.lock').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        target, version, digest, url = line.split()
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid Python digest')
        records[target] = dict(version=version, sha256=digest, url=url)
    return records


def host_target():
    return release_target(PlatformInfo.detect().target).name


def platform_tags(target):
    return release_target(target).wheel_platforms()


def windows_requirements(source, destination, version):
    """pip --platform selects wheels, but does not change marker evaluation."""
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    environment = default_environment()
    environment.update(os_name='nt', sys_platform='win32', platform_system='Windows',
                       platform_machine='AMD64', python_full_version=version,
                       python_version='.'.join(version.split('.')[:2]),
                       implementation_name='cpython', implementation_version=version)
    result = []
    for line in source.read_text().replace('\\\n', ' ').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        requirement, separator, hashes = line.partition('--hash=')
        parsed = Requirement(requirement.strip())
        if parsed.marker is not None and not parsed.marker.evaluate(environment):
            continue
        parsed.marker = None
        if not separator:
            raise ValueError('Windows requirements must remain hash-locked')
        result.append(str(parsed) + ' --hash=' + hashes)
    destination.write_text('\n'.join(result) + '\n')


def prepare(root, output, target, *, with_dev=False, archive=None, wheelhouse=None, offline=False):
    record = runtime_records(root)[target]
    if sys.version_info[:2] != tuple(map(int, record['version'].split('.')[:2])):
        raise ValueError('跨平台准备依赖时请使用锁定的 Python 主次版本，以正确求值环境标记')
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('输出目录必须为空，避免混入旧版本依赖')
    output.mkdir(parents=True, exist_ok=True)
    # Build in temporary staging: never publish a half-complete kit.
    with tempfile.TemporaryDirectory(prefix='.python-kit-', dir=output.parent) as temporary:
        stage = Path(temporary)
        runtime = stage / 'runtime'
        runtime.mkdir()
        packed = runtime / 'python.tar.gz'
        if archive:
            shutil.copyfile(archive, packed)
        elif offline:
            raise ValueError('离线准备需要 --runtime-archive')
        else:
            with urllib.request.urlopen(record['url'], timeout=120) as response:
                with packed.open('wb') as f:
                    shutil.copyfileobj(response, f)
        with packed.open('rb') as f:
            actual = hashlib.file_digest(f, 'sha256').hexdigest()
        if actual != record['sha256']:
            raise ValueError('Python SHA256 mismatch')
        if target == 'windows-x86_64':
            # PowerShell can start this directly: no host Python, tar or network
            # is required to bootstrap an extracted Windows ZIP release.
            extract_files(packed, runtime)
            if not (runtime / release_target(target).runtime_python).is_file():
                raise ValueError('Windows Python archive is missing python/python.exe')
            packed.unlink()
        shutil.copy2(root / 'runtime/python.lock', runtime / 'python.lock')
        (runtime / 'target').write_text(target + '\n')
        dependencies = stage / 'wheelhouse'
        dependencies.mkdir()
        locks = ['requirements-core.lock' if target == 'windows-x86_64' else 'requirements-lsp.lock']
        if with_dev:
            locks += ['requirements-build.lock', 'requirements-dev.lock']
        command = [sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check',
                   '--require-hashes', '--only-binary=:all:', '--implementation', 'cp',
                   '--python-version', record['version'],
                   '--abi', 'cp' + ''.join(record['version'].split('.')[:2]),
                   '--dest', str(dependencies)]
        for tag in platform_tags(target):
            command += ['--platform', tag]
        if offline:
            if wheelhouse is None:
                raise ValueError('离线准备需要 --wheelhouse')
            command += ['--no-index', '--no-cache-dir']
        if wheelhouse:
            command += ['--find-links', str(Path(wheelhouse).resolve(strict=True))]
        for name in locks:
            requirements = root / name
            if target == 'windows-x86_64':
                requirements = stage / name
                windows_requirements(root / name, requirements, record['version'])
            command += ['-r', str(requirements)]
        if target == 'windows-x86_64':
            # Every applicable transitive dependency is already explicitly locked.
            command += ['--no-deps']
        subprocess.run(command, check=True)
        for name in locks:
            (stage / name).unlink(missing_ok=True)
        (stage / 'bundle.json').write_text(json.dumps({
            'target': target, 'python': record['version'], 'development': with_dev,
            'requirements': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                             for name in locks},
        }, indent=2) + '\n')
        for path in stage.iterdir():
            shutil.move(str(path), output / path.name)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=tuple(runtime_records()), default=host_target())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--with-dev', action='store_true')
    parser.add_argument('--runtime-archive', type=Path)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    prepare(ROOT, args.output, args.target, with_dev=args.with_dev,
            archive=args.runtime_archive, wheelhouse=args.wheelhouse, offline=args.offline)
    print('Python 与离线依赖已准备：' + str(args.output))


if __name__ == '__main__':
    main()
