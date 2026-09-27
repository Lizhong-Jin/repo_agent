"""Pinned online/offline wheel installation; no implicit source builds."""

import platform
from pathlib import Path


def source_flags(wheelhouse=None, *, offline=False):
    flags = ['--disable-pip-version-check']
    if offline:
        flags += ['--no-index', '--no-cache-dir']
    if wheelhouse is not None:
        path = Path(wheelhouse).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError('wheelhouse 必须是目录')
        flags += ['--find-links', str(path)]
    elif offline:
        raise ValueError('离线安装需要 --wheelhouse 或发行包内的 wheelhouse')
    return flags


def requirement_files(root, *, native, source):
    names = ['requirements-lsp.lock' if native else 'requirements-core.lock']
    if source:
        names += ['requirements-build.lock', 'requirements-dev.lock']
    return [Path(root) / name for name in names]


def requirement_args(files):
    return [value for path in files for value in ('-r', str(path))]


def verify_bundle_platform(root):
    marker = Path(root) / 'runtime/target'
    if not marker.exists():
        return
    system = {'Darwin': 'macos', 'Linux': 'linux'}.get(platform.system())
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64'}.get(platform.machine())
    if marker.read_text().strip() != f'{system}-{arch}':
        raise ValueError('安装包平台不匹配')
