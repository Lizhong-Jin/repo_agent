"""Pinned online/offline wheel installation; no implicit source builds."""

from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()
    __package__ = "installer"

from host_support.platforms import PlatformInfo


def source_flags(wheelhouse=None, *, offline=False):
    flags = ["--disable-pip-version-check"]
    if offline:
        flags += ["--no-index", "--no-cache-dir"]
    if wheelhouse is not None:
        path = Path(wheelhouse).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError("wheelhouse 必须是目录")
        flags += ["--find-links", str(path)]
    elif offline:
        raise ValueError("离线安装需要 --wheelhouse 或发行包内的 wheelhouse")
    return flags


def requirement_files(root, *, native, source):
    names = ["requirements-lsp.lock" if native else "requirements-core.lock"]
    if source:
        names += ["requirements-build.lock", "requirements-dev.lock"]
    return [Path(root) / name for name in names]


def requirement_args(files):
    return [value for path in files for value in ("-r", str(path))]


def verify_bundle_platform(root):
    marker = Path(root) / "runtime/target"
    if not marker.exists():
        return
    if marker.read_text().strip() != PlatformInfo.detect().target:
        raise ValueError("安装包平台不匹配")
