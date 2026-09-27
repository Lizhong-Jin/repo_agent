"""Application directories and interpreter layouts, without probing executables."""

import os
import sys
from pathlib import Path


def app_directory(kind):
    # Preserve the existing macOS/Linux XDG locations and override semantics.
    variable, fallback = {
        "config": ("XDG_CONFIG_HOME", ".config"),
        "state": ("XDG_STATE_HOME", ".local/state"),
        "data": ("XDG_DATA_HOME", ".local/share"),
        "cache": ("XDG_CACHE_HOME", ".cache"),
    }[kind]
    return (
        Path(os.environ.get(variable) or Path.home() / fallback).expanduser().resolve()
        / "repo-agent"
    )


def user_config_path():
    override = os.environ.get("AGENT_CONFIG_DIR")
    directory = Path(override).expanduser().resolve() if override else app_directory("config")
    return directory / ".env"


def session_state_root():
    return app_directory("state") / "sessions"


def user_bin_dir():
    return Path.home() / ".local/bin"


def scripts_dir(prefix, *, platform=None):
    return Path(prefix) / ("Scripts" if (platform or sys.platform) == "win32" else "bin")


def environment_python(prefix, *, kind="venv", platform=None):
    selected = platform or sys.platform
    if selected == "win32":
        return (
            Path(prefix) if kind == "conda" else scripts_dir(prefix, platform=selected)
        ) / "python.exe"
    return scripts_dir(prefix, platform=selected) / "python"


def installed_python(root):
    return environment_python(Path(root) / ".venv")


def installed_command(root, name):
    suffix = ".exe" if sys.platform == "win32" else ""
    return scripts_dir(Path(root) / ".venv") / (name + suffix)


def public_command(bin_dir, name):
    return Path(bin_dir) / (name + (".exe" if os.name == "nt" else ""))


def find_windows_executable(name, *, exclude=()):
    """Search explicit absolute PATH entries; never implicitly execute from cwd.

    shutil.which on some supported Windows Python versions prepends cwd even
    when a PATH is supplied. Host-side snapshot creation must not run project
    executables. Only native .exe commands are accepted here.
    """
    roots = tuple(Path(root).resolve() for root in exclude)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        directory = Path(entry.strip('"'))
        if not directory.is_absolute():
            continue
        try:
            candidate = (directory / (name + ".exe")).resolve()
            if any(candidate.is_relative_to(root) for root in roots):
                continue
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None
