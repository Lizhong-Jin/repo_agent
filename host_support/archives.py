"""Portable archive member names, independent of the build host's path syntax."""

from pathlib import PurePosixPath, PureWindowsPath


def archive_path(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ValueError("Invalid archive path")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or PureWindowsPath(name).drive:
        raise ValueError("Invalid archive path")
    return path
