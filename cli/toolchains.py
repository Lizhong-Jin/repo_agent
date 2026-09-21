"""List native language dependencies and fill missing services after installation."""

import argparse
import os
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

from .dependencies import (
    LANGUAGES,
    install_language_servers,
    language_status,
    native_preflight,
    prepare_toolchains,
    print_language_status,
    service_report,
)
from .install_transaction import TRANSACTION
from .installation import load_record, registry_dir, save_record
from .maintenance import file_lock, print_report


def install_server(root, language):
    """Download JS/Go into a staging directory; preserve existing files on failure."""
    if language == "python":
        python = str(root / ".venv/bin/python")
        subprocess.run([python, "-m", "pip", "install", "python-lsp-server>=1.12,<2"], check=True)
        subprocess.run([python, "-m", "pip", "check"], check=True)
        return
    if language not in {"go", "typescript"}:
        return  # clangd is supplied by the LLVM toolchain.
    with tempfile.TemporaryDirectory(prefix=".toolchain-", dir=root / ".venv") as temporary:
        staging = Path(temporary)
        (staging / ".venv/bin").mkdir(parents=True)
        install_language_servers(staging, [language])
        relative = "bin/gopls" if language == "go" else "lsp"
        source, target = staging / ".venv" / relative, root / ".venv" / relative
        if not source.exists():
            raise ValueError(f"{language} 下载未产生预期的语言服务")
        if target.is_symlink():
            raise ValueError(f"语言服务目录或文件是外部链接，未替换：{target}")
        backup = staging / "previous"
        if target.exists():
            target.rename(backup)
        try:
            os.replace(source, target)
            if not print_report(service_report(root, mode="native", languages=[language])):
                raise ValueError(f"{language} 语言服务实测失败")
        except BaseException:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if backup.exists():
                backup.rename(target)
            raise


def install_missing(root, languages):
    with ExitStack() as stack:
        stack.enter_context(file_lock(registry_dir().parent / ".maintenance.lock"))
        stack.enter_context(file_lock(root / ".repo-agent-operation.lock"))
        if (root / TRANSACTION).exists():
            raise ValueError("有尚未恢复的安装；先在安装目录运行 ./install.sh --recover")
        record = load_record(root)
        if not record or record["status"] != "installed":
            raise ValueError("未找到完成的安装记录；请先运行 ./install.sh")
        venv = root / ".venv"
        if venv.is_symlink() or not venv.is_dir():
            raise ValueError("虚拟环境不可用；请重新运行 ./install.sh")
        identity = venv.stat()
        if record.get("venv") != {"device": identity.st_dev, "inode": identity.st_ino}:
            raise ValueError("虚拟环境已被替换；请重新运行 ./install.sh")
        if not print_report(native_preflight()):
            raise ValueError(
                "此补齐命令用于 macOS/Linux native 模式；Docker 依赖请用 repo-agent-build-sandbox"
            )
        print(
            "按需补齐宿主机依赖，不改变默认运行模式。系统共享工具链会保留。",
            flush=True,
        )
        for language in languages:
            status = next(row for row in language_status(root) if row["language"] == language)
            if not status["toolchain"]:
                prepare_toolchains(root / ".venv/bin/python", language)
            # Recheck: installing LLVM already supplies clangd.
            status = next(row for row in language_status(root) if row["language"] == language)
            if status["service"]:
                print(f"{language} 语言服务已安装，跳过下载。", flush=True)
            else:
                print(f"补齐 {language} 语言服务……", flush=True)
                install_server(root, language)
            if language not in {"go", "typescript"} or status["service"]:
                if not print_report(service_report(root, mode="native", languages=[language])):
                    raise ValueError(
                        f"{language} 语言服务实测失败；请运行 repo-agent doctor --mode native"
                    )
            record["languages"] = [
                name for name in LANGUAGES if name in {*record.get("languages", []), language}
            ]
            save_record(record)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="repo-agent toolchains",
        description="查看及补齐宿主机语言工具链和语言服务（无需模型配置）",
    )
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("list", help="列出支持的语言、工具链和语言服务安装状态")
    install = commands.add_parser("install", help="补齐指定语言；已有可用依赖不重复下载")
    install.add_argument("languages", nargs="+", choices=["all", *LANGUAGES])
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.action == "install":
            languages = (
                list(LANGUAGES) if "all" in args.languages else list(dict.fromkeys(args.languages))
            )
            install_missing(root, languages)
            print("所选语言已补齐并通过符号查询验证；重启 Agent 使用。")
        print_language_status(root)
    except KeyboardInterrupt:
        parser.exit(1, "已取消补齐；此前已完成的语言和共享工具链保留。\n")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            f"工具链操作未完成：{error}\n可重试相同命令；此前已完成的语言和共享工具链保留。\n",
        )


if __name__ == "__main__":
    main()
