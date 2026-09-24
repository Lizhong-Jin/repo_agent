"""One-time user installation; no configuration is copied into task projects."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

if __package__:
    from .installation import (
        COMMANDS,
        begin_install,
        load_record,
        prepare_venv,
        record_image,
        registry_dir,
        save_record,
        user_config_path,
    )
else:
    # install.sh runs this with the base interpreter before any dependencies exist.
    from installation import (
        COMMANDS,
        begin_install,
        load_record,
        prepare_venv,
        record_image,
        registry_dir,
        save_record,
        user_config_path,
    )


if __package__:
    from .config_storage import config_lock
    from .dependencies import (
        available_mode,
        language_status,
        native_preflight,
        preparation_report,
        print_language_status,
        selected_languages,
        service_report,
    )
    from .install_network import network_options, run_download
    from .install_transaction import TRANSACTION, InstallTransaction, recover_install
    from .maintenance import environment_report, file_lock, print_report
    from .toolchains import install_missing
else:
    from config_storage import config_lock
    from install_network import network_options, run_download
    from install_transaction import TRANSACTION, InstallTransaction, recover_install
    from maintenance import environment_report, file_lock, print_report
    from toolchains import install_missing

    from dependencies import (
        available_mode,
        language_status,
        native_preflight,
        preparation_report,
        print_language_status,
        selected_languages,
        service_report,
    )


def command_state(command: Path) -> str | None:
    """Snapshot the link itself, including dangling links; reject unrelated files."""
    if command.is_symlink():
        return os.readlink(command)
    if command.exists():
        raise ValueError(f"保留已有命令 {command}（不是安装链接）；请用 --bin-dir 选择其他目录")
    return None


def confirm_commands(agent_home: Path, bin_dir: Path) -> dict[str, str | None]:
    states = {}
    replacements = []
    for name in COMMANDS:
        command = bin_dir / name
        previous = command_state(command)
        states[name] = previous
        if previous is None:
            continue
        old_target = Path(os.path.abspath(command.parent / previous))
        target = agent_home / ".venv/bin" / name
        if old_target == target:
            continue
        if old_target.name != name or old_target.parent.parts[-2:] != (".venv", "bin"):
            raise ValueError(
                f"保留已有命令 {command}（无法识别为旧安装）；请用 --bin-dir 选择其他目录"
            )
        replacements.append((command, old_target, target))
    if replacements:
        print("检测到其他目录的 Repo Agent 安装，安装成功后将切换以下命令：", flush=True)
        for command, old_target, target in replacements:
            print(f"  {command}\n    当前：{old_target}\n    新位置：{target}", flush=True)
        print("旧安装目录和用户配置会保留；卸载新安装后不会自动回退。", flush=True)
        try:
            answer = input("是否继续安装并替换命令？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in {"y", "yes"}:
            raise ValueError("已取消安装，未修改旧安装或命令")
    return states


def check_command_states(bin_dir: Path, states: dict[str, str | None]) -> None:
    for name, previous in states.items():
        if command_state(bin_dir / name) != previous:
            raise ValueError(f"安装期间命令发生变化，未覆盖：{bin_dir / name}；请重新运行安装")


def configure_user(agent_home: Path, record: dict | None = None) -> Path:
    """Create an editable template without prompting or importing credentials."""
    path = user_config_path()
    if path.exists():
        print(f"保留已有配置：{path}")
        return path
    template = (agent_home / ".env.example").read_text(encoding="utf-8")
    if record is not None:
        record["config_created"] = True
        save_record(record)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with config_lock(path):
        if path.exists():
            print(f"保留已有配置：{path}")
            return path
        # Publish a complete template exclusively; a failed write never leaves half a .env.
        fd, temporary = tempfile.mkstemp(prefix=".config-template-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(template)
            os.link(temporary, path)  # Also refuses dangling symlinks and concurrent creation.
        finally:
            Path(temporary).unlink(missing_ok=True)
    print(f"已创建默认配置：{path}")
    return path


def install_command(
    agent_home: Path,
    bin_dir: Path,
    name: str = "repo-agent",
    record: dict | None = None,
    *,
    approved_states: dict[str, str | None] | None = None,
) -> Path:
    target = agent_home / ".venv" / "bin" / name
    if not target.is_file():
        raise ValueError(f"未找到 {name} 入口，请通过 install.sh 安装")
    bin_dir.mkdir(parents=True, exist_ok=True)
    command = bin_dir / name
    previous = command_state(command)
    matches = previous is not None and Path(os.path.abspath(command.parent / previous)) == target
    if approved_states is not None:
        check_command_states(bin_dir, {name: approved_states[name]})
    elif not matches and previous is not None:
        raise ValueError(f"保留已有命令 {command}；请用 --bin-dir 选择其他目录")
    if record is not None:
        item = {"path": str(command), "target": str(target)}
        if item not in record["commands"]:
            record["commands"].append(item)
            save_record(record)
    if not matches:
        if previous is None:
            command.symlink_to(target)
        else:
            # Keep the old command available until the replacement link is ready.
            with tempfile.TemporaryDirectory(prefix=".repo-agent-", dir=bin_dir) as temporary:
                link = Path(temporary) / name
                link.symlink_to(target)
                check_command_states(bin_dir, {name: previous})
                os.replace(link, command)
    return command


def configure_path(bin_dir: Path, record: dict | None = None, transaction=None) -> list[Path]:
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
        if path.is_symlink():
            print(f"保留符号链接形式的 shell 配置，请手动设置 PATH：{path}")
            continue
        path = path.resolve()
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        if line not in previous.splitlines():
            if record is None:
                block = f"\n# Repo Agent\n{line}\n"
            else:
                block = (
                    f"\n# >>> Repo Agent {record['id']}\n{line}\n# <<< Repo Agent {record['id']}\n"
                )
                item = {
                    "path": str(path),
                    "bin_dir": str(bin_dir),
                    "block": block,
                    "created": not path.exists(),
                }
                if item not in record["shell"]:
                    record["shell"].append(item)
                    save_record(record)
            if transaction is not None:
                transaction.append_shell(path, block)
            with path.open("a", encoding="utf-8") as output:
                output.write(block)
    return files


def choose_toolchains(value):
    if value is not None:
        return value
    print(
        "Python 语言服务默认安装。其他语言需要额外下载语言服务；"
        "macOS 缺少 Node.js、Go、LLVM 时使用 Homebrew 补齐，Linux 请先用发行版包管理器安装。"
    )
    try:
        return input("是否补齐额外工具链和语言服务？[y/N] ").strip().lower() in {
            "y",
            "yes",
        }
    except EOFError:
        return False


def preserve_managed_servers(transaction, root):
    """Keep previously downloaded servers across a clean Python environment rebuild."""
    old = transaction.directory / "venv"
    for relative in ("lsp", "bin/gopls"):
        source, target = old / relative, root / ".venv" / relative
        if source.is_symlink() or not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def main(argv=None, *, approved_commands=None) -> None:
    parser = argparse.ArgumentParser(description="配置 Repo Agent 用户安装")
    parser.add_argument("--agent-home", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local" / "bin")
    parser.add_argument(
        "--skip-sandbox", action="store_true", help="兼容选项：Docker 模式跳过镜像构建"
    )
    parser.add_argument(
        "--mode",
        choices=["native", "docker", "local"],
        help="默认沿用本安装模式；首次为 native",
    )
    parser.add_argument(
        "--languages",
        default="all",
        help="native 语言服务：all 或 python,typescript,go,cpp 的组合",
    )
    toolchains = parser.add_mutually_exclusive_group()
    toolchains.add_argument(
        "--with-toolchains",
        dest="toolchains",
        action="store_true",
        help="无需询问，补齐所选语言的工具链和语言服务",
    )
    toolchains.add_argument(
        "--skip-toolchains",
        dest="toolchains",
        action="store_false",
        help="无需询问，跳过额外补齐；Python 服务仍安装",
    )
    parser.set_defaults(toolchains=None)
    parser.add_argument("--no-path", action="store_true")
    parser.add_argument("--bootstrap", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wheel", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="只检查安装环境，不做修改")
    parser.add_argument("--recover", action="store_true", help="只恢复上次中断的安装")
    args = parser.parse_args(argv)
    try:
        agent_home = args.agent_home.expanduser().resolve(strict=True)
        bin_dir = args.bin_dir.expanduser().resolve()
        args.mode = args.mode or available_mode(agent_home)
        selected_languages(args.languages)
        release = None
        if args.wheel:
            if __package__:
                from .release_manifest import read_release
            else:
                from release_manifest import read_release
            release = read_release(agent_home)
            if args.wheel.resolve() != (agent_home / release["wheel"]).resolve():
                raise ValueError("wheel 与发行清单不匹配")
        network_options()
        extra_languages = []
        unfinished_languages = []
        build_docker = args.mode == "docker" and not args.skip_sandbox

        def preflight():
            rows = environment_report(agent_home, docker=build_docker)
            if args.mode == "native":
                rows += native_preflight()
                if not any(level == "ERROR" for level, _, _ in rows):
                    optional = preparation_report(agent_home / ".venv/bin/python", args.languages)
                    if not (args.check and args.toolchains):
                        optional = [
                            (
                                "WARN" if level == "ERROR" else level,
                                name,
                                detail.replace(
                                    "安装时将通过 Homebrew 补齐",
                                    "选择补齐后通过 Homebrew 安装",
                                ),
                            )
                            for level, name, detail in optional
                        ]
                    rows += optional
            rows.append(("OK", "安装模式", args.mode))
            return rows

        if args.check:
            if not print_report(preflight()):
                parser.exit(1)
            return
        pending = (agent_home / TRANSACTION).exists()
        approved_states = approved_commands
        if not args.recover and not pending and approved_states is None:
            approved_states = confirm_commands(agent_home, bin_dir)
        with ExitStack() as stack:
            stack.enter_context(file_lock(registry_dir().parent / ".maintenance.lock"))
            stack.enter_context(file_lock(agent_home / ".repo-agent-operation.lock"))
            # Recovery has its own recorded bin directory. The registry lock serializes it
            # with uninstall and installations using the same user state directory.
            recover_install(agent_home)
            if args.recover:
                print("安装恢复检查完成。")
                return
            if args.bootstrap and args.mode == "native":
                args.toolchains = (
                    choose_toolchains(args.toolchains) if args.languages != "python" else False
                )
            if args.bootstrap and not print_report(preflight()):
                raise ValueError("安装环境检查未通过；修复上面的 ERROR 后重试")
            if approved_states is None:
                approved_states = confirm_commands(agent_home, bin_dir)
            stack.enter_context(file_lock(bin_dir / ".repo-agent-install.lock"))
            check_command_states(bin_dir, approved_states)
            transaction = InstallTransaction(agent_home, bin_dir, approved_states)
            try:
                record = begin_install(agent_home)
                if str(bin_dir) not in record.setdefault("bin_dirs", []):
                    record["bin_dirs"].append(str(bin_dir))
                    save_record(record)
                record["mode"] = args.mode
                record["uses_default_image"] = args.mode == "docker"
                python = sys.executable
                languages = []
                if args.bootstrap:
                    transaction.fresh_venv()
                    prepare_venv(record)
                    subprocess.run([python, "-m", "venv", str(agent_home / ".venv")], check=True)
                    python = str(agent_home / ".venv/bin/python")
                    print("安装核心依赖……", flush=True)
                    lock = agent_home / (
                        "requirements-lsp.lock"
                        if args.mode == "native"
                        else "requirements-core.lock"
                    )
                    locked = [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--require-hashes",
                        "--only-binary=:all:",
                        "-r",
                        str(lock),
                    ]
                    if not args.wheel:
                        locked += [
                            "-r",
                            str(agent_home / "requirements-build.lock"),
                            "-r",
                            str(agent_home / "requirements-dev.lock"),
                        ]
                        print("源码安装：自动准备开发依赖（pytest、Ruff）。", flush=True)
                    run_download(locked, label="安装固定版本依赖", cwd=agent_home)
                    if args.wheel:
                        run_download(
                            [
                                python,
                                "-m",
                                "pip",
                                "install",
                                "--no-deps",
                                "--no-index",
                                str(args.wheel.resolve()),
                            ],
                            label="安装发行版 wheel",
                            cwd=agent_home,
                        )
                    else:
                        run_download(
                            [
                                python,
                                "-m",
                                "pip",
                                "install",
                                "-e",
                                str(agent_home) + ("[lsp]" if args.mode == "native" else ""),
                                "--no-deps",
                                "--no-build-isolation",
                            ],
                            label="安装 Agent 核心依赖",
                            cwd=agent_home,
                        )
                    subprocess.run([python, "-m", "pip", "check"], check=True)
                    if args.mode == "native":
                        preserve_managed_servers(transaction, agent_home)
                        states = {row["language"]: row for row in language_status(agent_home)}
                        languages = ["python"]
                        previous = [
                            name
                            for name in record.get("languages", [])
                            if name in {"typescript", "go", "cpp"}
                        ]
                        extra_languages = (
                            [
                                name
                                for name in selected_languages(args.languages)
                                if name != "python"
                            ]
                            if args.toolchains
                            else [
                                name
                                for name in previous
                                if states[name]["service"] and states[name]["toolchain"]
                            ]
                        )
                        record["pending_languages"] = list(
                            dict.fromkeys(
                                [
                                    *record.get("pending_languages", []),
                                    *previous,
                                    *extra_languages,
                                ]
                            )
                        )
                        print("验证原生沙箱和 Python 语言服务（使用临时示例文件）……", flush=True)
                        if not print_report(
                            service_report(agent_home, mode="native", languages=languages)
                        ):
                            raise ValueError("原生模式依赖实测未通过，恢复原安装")
                    subprocess.run(
                        [python, "-I", "-c", "import cli.main, sandbox.build"],
                        cwd=agent_home,
                        check=True,
                    )
                    for name in COMMANDS:
                        subprocess.run(
                            [str(agent_home / ".venv/bin" / name), "--help"],
                            cwd=agent_home,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            timeout=30,
                        )
                config_path = configure_user(agent_home, record)
                if build_docker:
                    build = [python, "-I", "-m", "sandbox.build"]
                    if args.bootstrap:
                        build += ["--image", transaction.stage_image()]
                    subprocess.run(build, cwd=agent_home, check=True)
                    if args.bootstrap:
                        transaction.promote_image()
                    record_image(agent_home, "repo-agent-sandbox:v1")
                    record = load_record(agent_home)
                    record["images"] = [
                        item
                        for item in record["images"]
                        if item["tag"] != transaction.state["staging_image"]
                    ]
                for name in COMMANDS:
                    if not (agent_home / ".venv/bin" / name).is_file():
                        raise ValueError(f"未找到 {name} 入口，请通过 install.sh 安装")
                check_command_states(bin_dir, approved_states)
                commands = []
                for name in COMMANDS:
                    transaction.change_link(name)
                    commands.append(
                        install_command(
                            agent_home,
                            bin_dir,
                            name,
                            record,
                            approved_states=approved_states,
                        )
                    )
                files = configure_path(bin_dir, record, transaction) if not args.no_path else []
                record["mode"] = args.mode
                record["uses_default_image"] = args.mode == "docker"
                record["languages"] = languages
                record["status"] = "installed"
                record["kind"] = "release" if release else "development"
                if release:
                    record["app_version"] = release["version"]
                    record["release_files"] = release["files"]
                record["python"] = {
                    "executable": sys.executable,
                    "version": sys.version.split()[0],
                }
                save_record(record)
                transaction.commit()
            except BaseException:
                if not transaction.state["committed"]:
                    transaction.rollback()
                raise
    except KeyboardInterrupt:
        parser.exit(1, "安装已取消。\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"安装未完成：{error}\n")
    print("Agent 核心安装成功。", flush=True)
    if extra_languages:
        print("开始补齐或验证额外语言服务；失败不会撤销核心安装。", flush=True)
        for index, language in enumerate(extra_languages):
            try:
                install_missing(agent_home, [language])
            except KeyboardInterrupt:
                unfinished_languages.extend(extra_languages[index:])
                print("已取消额外补齐；Agent 核心安装保留。", flush=True)
                break
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                unfinished_languages.append(language)
                print(
                    f"[WARN] {language} 未完成：{error}\n重试：repo-agent toolchains install {language}",
                    flush=True,
                )
        try:
            languages = load_record(agent_home).get("languages", languages)
        except (OSError, ValueError, AttributeError):
            print("[WARN] 无法刷新语言服务记录，请用 repo-agent doctor 检查。")
    for command in commands:
        print(f"已安装命令：{command}")
    print(f"默认启动模式：{args.mode}（可用 --sandbox 显式切换）")
    if args.mode == "native":
        print("已验证的原生模式语言服务：" + ", ".join(languages))
        print("系统共享工具链在卸载或安装失败时保留；虚拟环境内的语言服务跟随安装恢复和卸载。")
    try:
        print_language_status(agent_home)
    except (KeyboardInterrupt, OSError, ValueError, subprocess.SubprocessError):
        print("[WARN] 状态列表未完成；可运行 repo-agent toolchains list，核心安装已保留。")
    if unfinished_languages:
        print("安装成功，以下额外语言尚未完成：" + ", ".join(unfinished_languages))
        print("稍后补齐：repo-agent toolchains install " + " ".join(unfinished_languages))
    if args.mode == "docker":
        print("以后重建镜像：repo-agent-build-sandbox")
    print(f"首次启动前可编辑 {config_path}；也可直接启动 repo-agent，由向导设置模型和 API Key。")
    if files:
        print("已配置 PATH；打开新终端后，在任意项目目录执行 repo-agent。")
    else:
        print("请确认命令目录已在 PATH 中。")
    print(f'当前终端立即使用：export PATH={shlex.quote(str(bin_dir))}:"$PATH"')
    if args.skip_sandbox and args.mode == "docker":
        print("已跳过镜像构建；Docker 模式仍需要镜像。仅文件操作可用 repo-agent --sandbox local。")


if __name__ == "__main__":
    main()
