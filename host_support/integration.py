"""POSIX command entries and shell PATH plans; callers journal every mutation."""

import os
import shlex
from pathlib import Path


def command_state(command):
    if os.name == "nt":
        from .windows_install import command_state as windows_state

        return windows_state(command)
    if command.is_symlink():
        return os.readlink(command)
    if command.exists():
        raise ValueError(f"保留已有命令 {command}（不是安装链接）；请用 --bin-dir 选择其他目录")
    return None


def shell_path_plan(bin_dir):
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "zsh":
        files = [Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"]
    elif shell == "bash":
        login = next(
            (
                Path.home() / name
                for name in (".bash_profile", ".bash_login", ".profile")
                if (Path.home() / name).exists()
            ),
            Path.home() / ".bash_profile",
        )
        files = [Path.home() / ".bashrc", login]
    else:
        files = []
    return files, f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"'
