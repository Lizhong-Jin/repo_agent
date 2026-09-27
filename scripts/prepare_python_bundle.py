"""Prepare a platform-specific Python + wheelhouse kit without running its Python."""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    systems = {'Darwin': 'macos', 'Linux': 'linux'}
    arches = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64', 'AMD64': 'x86_64'}
    try:
        return systems[platform.system()] + '-' + arches[platform.machine()]
    except KeyError as error:
        raise ValueError('Supported targets: macOS/Linux, arm64/x86_64') from error


def platform_tags(target):
    system, arch = target.split('-')
    if system == 'linux':
        arch = 'aarch64' if arch == 'arm64' else arch
        # Promise glibc >= 2.28; include older compatible wheels as fallback.
        return [f'manylinux_2_{minor}_{arch}' for minor in range(28, 16, -1)] + [
            f'manylinux2014_{arch}']
    from packaging.tags import mac_platforms
    return list(mac_platforms((11, 0) if arch == 'arm64' else (10, 15), arch))


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
        shutil.copy2(root / 'runtime/python.lock', runtime / 'python.lock')
        (runtime / 'target').write_text(target + '\n')
        dependencies = stage / 'wheelhouse'
        dependencies.mkdir()
        locks = ['requirements-lsp.lock']
        if with_dev:
            locks += ['requirements-build.lock', 'requirements-dev.lock']
        command = [sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check',
                   '--require-hashes', '--only-binary=:all:', '--implementation', 'cp',
                   '--python-version', record['version'], '--abi', 'cp313',
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
            command += ['-r', str(root / name)]
        subprocess.run(command, check=True)
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
