"""One-time user installation; no configuration is copied into task projects."""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .config import user_config_path


def configure_user(agent_home: Path) -> Path:
    """Create an editable template without prompting or importing credentials."""
    path = user_config_path()
    if path.exists():
        print(f"保留已有配置：{path}")
        return path
    template = (agent_home / ".env.example").read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation also refuses a pre-existing dangling symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(template)
    print(f"已创建默认配置：{path}")
    return path


def install_command(agent_home: Path, bin_dir: Path) -> Path:
    target = agent_home / ".venv" / "bin" / "repo-agent"
    if not target.is_file():
        raise ValueError("未找到 repo-agent 入口，请通过 install.sh 安装")
    bin_dir.mkdir(parents=True, exist_ok=True)
    command = bin_dir / "repo-agent"
    if command.is_symlink() and command.resolve() == target.resolve():
        return command
    if command.exists() or command.is_symlink():
        raise ValueError(f"保留已有命令 {command}；请用 --bin-dir 选择其他目录")
    command.symlink_to(target)
    return command


def configure_path(bin_dir: Path) -> list[Path]:
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
        return []
    line = f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"'
    for path in files:
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in previous.splitlines():
            with path.open("a", encoding="utf-8") as output:
                output.write(f"\n# Repo Agent\n{line}\n")
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description="配置 Repo Agent 用户安装")
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument("--skip-sandbox", action="store_true")
    # Compatibility only: model configuration is always deferred until after installation.
    parser.add_argument("--non-interactive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-path", action="store_true")
    args = parser.parse_args()
    try:
        agent_home = args.agent_home.expanduser().resolve(strict=True)
        bin_dir = args.bin_dir.expanduser().resolve()
        config_path = configure_user(agent_home)
        if not args.skip_sandbox:
            subprocess.run([sys.executable, "-m", "sandbox.build"], cwd=agent_home, check=True)
        command = install_command(agent_home, bin_dir)
        files = configure_path(bin_dir) if not args.no_path else []
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"安装未完成：{error}\n")
    print(f"已安装命令：{command}")
    print(f"首次启动前，请编辑 {config_path}，填写 LLM_PROVIDER、LLM_MODEL 和对应 API Key。")
    if files:
        print("已配置 PATH；打开新终端后，在任意项目目录执行 repo-agent。")
    else:
        print("请确认命令目录已在 PATH 中。")
    print(f'当前终端立即使用：export PATH={shlex.quote(str(bin_dir))}:"$PATH"')
    if args.skip_sandbox:
        print("已跳过镜像构建；Docker 模式仍需要镜像。仅文件操作可用 repo-agent --sandbox local。")


if __name__ == "__main__":
    main()
