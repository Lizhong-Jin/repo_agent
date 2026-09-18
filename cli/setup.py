"""One-time user installation; no configuration is copied into task projects."""

import argparse
import getpass
import os
import shlex
import subprocess
import sys
from pathlib import Path

from llm import ConfigurationError
from llm.providers import PROVIDERS, get_provider

from .config import read_config, user_config_path


def configure_user(*, interactive: bool) -> Path:
    path = user_config_path()
    if path.exists():
        values = read_config(path)
        provider = get_provider(values.get("LLM_PROVIDER") or "deepseek")
        if not values.get("LLM_MODEL") or not (
            values.get(provider.api_key_env) or os.environ.get(provider.api_key_env)
        ):
            raise ValueError(f"请在 {path} 中补齐 LLM_MODEL 和 {provider.api_key_env} 后重试")
        print(f"保留已有配置：{path}")
        return path
    name = os.environ.get("LLM_PROVIDER") or "deepseek"
    model = os.environ.get("LLM_MODEL") or ""
    base_url = os.environ.get("LLM_BASE_URL") or ""
    if interactive:
        print("支持的厂商：" + ", ".join(PROVIDERS))
        name = input(f"模型厂商 [{name}]：").strip() or name
    provider = get_provider(name)
    if interactive:
        model = input(f"模型 ID [{model or '必填'}]：").strip() or model
        base_url = input(f"API 基址 [{base_url or '厂商默认'}]：").strip() or base_url
    key = os.environ.get(provider.api_key_env) or ""
    if interactive and not key:
        key = getpass.getpass(f"{provider.api_key_env}（输入不回显）：").strip()
    if not model or not key:
        raise ValueError(f"请设置 LLM_MODEL 和 {provider.api_key_env}，或在终端交互安装")
    values = {
        "LLM_PROVIDER": provider.name,
        "LLM_MODEL": model,
        "LLM_BASE_URL": base_url,
        provider.api_key_env: key,
    }
    for value in values.values():
        if any(char in value for char in "\r\n\x00"):
            raise ValueError("配置值不能包含换行或空字符")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation also refuses a pre-existing dangling symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("# Repo Agent 用户配置；项目 .env 可覆盖这些设置。\n")
        for name, value in values.items():
            output.write(f'{name}="{value}"\n')
    print(f"已保存用户配置：{path}")
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
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--no-path", action="store_true")
    args = parser.parse_args()
    try:
        agent_home = args.agent_home.expanduser().resolve(strict=True)
        bin_dir = args.bin_dir.expanduser().resolve()
        configure_user(interactive=not args.non_interactive and sys.stdin.isatty())
        if not args.skip_sandbox:
            subprocess.run([sys.executable, "-m", "sandbox.build"], cwd=agent_home, check=True)
        command = install_command(agent_home, bin_dir)
        files = configure_path(bin_dir) if not args.no_path else []
    except (
        OSError,
        ValueError,
        ConfigurationError,
        EOFError,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"安装未完成：{error}\n")
    print(f"已安装命令：{command}")
    if files:
        print("已配置 PATH；打开新终端后，在任意项目目录执行 repo-agent。")
    else:
        print("请确认命令目录已在 PATH 中。")
    print(f'当前终端立即使用：export PATH={shlex.quote(str(bin_dir))}:"$PATH"')
    if args.skip_sandbox:
        print("已跳过镜像构建；Docker 模式仍需要镜像。仅文件操作可用 repo-agent --sandbox local。")


if __name__ == "__main__":
    main()
