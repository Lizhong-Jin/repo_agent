"""CLI argument schema and cross-option validation; no runtime creation."""

import argparse
import os
import sys

from host_support.execution import backend_capabilities
from host_support.paths import user_config_path
from installer.dependencies import available_mode
from installer.paths import installation_root
from sandbox import SandboxPolicy
from sandbox.environment import DEFAULT_IMAGE

from .settings import add_runtime_arguments, verification_command, writeback_mode


def create_parser():
    parser = argparse.ArgumentParser(
        description="运行可读写项目文件的 Coding Agent",
        epilog=(
            f"未指定的模型和运行参数自动从配置读取。用户配置：{user_config_path()}。"
            "使用 repo-agent config show 查看配置，config edit 修改配置；"
            "toolchains list 查看语言服务；version 查看版本和安装位置，uninstall 卸载当前安装。"
        ),
        allow_abbrev=False,
    )
    parser.add_argument("task", nargs="?", help="需要完成的任务；省略时进入交互模式")
    sessions = parser.add_mutually_exclusive_group()
    sessions.add_argument(
        "--new-session", action="store_true", help="启动全新会话；默认恢复当前项目上次会话"
    )
    sessions.add_argument(
        "--session", metavar="会话", help="恢复本项目指定序号、完整 ID、名称或 latest 的会话"
    )
    parser.add_argument("--name", help="新会话名称；省略时使用项目内递增序号")
    parser.add_argument("--root", default=".", help="项目根目录，默认当前目录")
    parser.add_argument(
        "--mode",
        choices=["develop", "review"],
        default=None,
        help="权限模式：develop 开发；review 只读审查；省略时恢复会话权限",
    )
    parser.add_argument(
        "--workspace",
        choices=["direct", "worktree"],
        default=None,
        help="local/native 工作区：direct 原目录；worktree 独立 Git 工作区；省略时恢复会话选择",
    )
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER") or "deepseek")
    parser.add_argument("--model", default=os.getenv("LLM_MODEL"))
    parser.add_argument(
        "--configure-model", action="store_true", help="启动时选择供应商、输入模型和 API Key 并保存"
    )
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL") or None)
    parser.add_argument(
        "--sandbox",
        choices=["local", "native", "docker"],
        default=available_mode(installation_root()),
        help=(
            "执行方式：默认沿用安装模式（首次 Windows 为 local，其余 native）；"
            "native 原生隔离（Windows 逐调用副本回写）；local 无命令执行；docker 使用副本"
        ),
    )
    parser.add_argument("--sandbox-image", help=f"自定义沙箱镜像，默认 {DEFAULT_IMAGE}")
    parser.add_argument(
        "--sandbox-profile",
        choices=["auto", "standard", "cuda", "metal"],
        default="auto",
        help=(
            "默认 auto：Docker / Linux native 检测 NVIDIA GPU；Apple Silicon native 启用 Metal；"
            "standard 禁用 GPU，cuda / metal 强制要求对应 GPU"
        ),
    )
    parser.add_argument(
        "--sandbox-gpus",
        help="Docker / Linux native GPU：all、单个设备索引或 GPU UUID；WSL2 native 仅 all",
    )
    parser.add_argument("--sandbox-review", help="查看之前保留的 sandbox 会话目录")
    parser.add_argument("--apply", action="store_true", help="与 --sandbox-review 一起显式回写")
    parser.add_argument(
        "--sandbox-writeback",
        type=writeback_mode,
        default=None,
        help="仅 Docker：回写策略；配置项 AGENT_SANDBOX_WRITEBACK，默认 manual",
    )
    parser.add_argument(
        "--sandbox-verify-command",
        type=verification_command,
        default=os.getenv("AGENT_SANDBOX_VERIFY_COMMAND") or "",
        help="on-success 回写前在容器执行的 JSON 命令数组；配置项 AGENT_SANDBOX_VERIFY_COMMAND",
    )
    parser.add_argument("--restore-backup", help="与 --sandbox-review 一起使用，恢复备份 ID")
    add_runtime_arguments(parser)
    parser.add_argument("--project-python", help="native 项目 Python；默认自动发现启动环境")
    return parser


def parse_arguments(argv=None):
    parser = create_parser()
    args = parser.parse_args(argv)
    if args.session is not None:
        if not args.session.strip():
            parser.error("--session 不能为空")
        if args.name is not None:
            parser.error("--session 不能与 --name 一起使用；改名请使用 /rename")
    if args.compact_summary_tokens is not None or os.getenv("AGENT_COMPACT_SUMMARY_TOKENS"):
        print("提示：compact-summary-tokens 已弃用并忽略；压缩大小由 compact-target 指导。")
    return parser, args


def validate_execution_options(parser, args):
    if getattr(args, "mode", None) == "review":
        if args.workspace == "worktree":
            parser.error("审查模式不创建 worktree；请用 --root 指向已有工作区")
        if args.sandbox_review or args.apply or args.restore_backup:
            parser.error("审查模式不能执行沙箱回写或恢复备份")
        if args.sandbox_writeback == "on-success":
            parser.error("审查模式不能自动回写")
    if getattr(args, "workspace", None) == "worktree" and args.sandbox == "docker":
        parser.error("第一版 --workspace worktree 仅用于 local/native；Docker 已有独立副本")
    if args.apply:
        parser.error("--apply 必须与 --sandbox-review 一起使用")
    if args.restore_backup:
        parser.error("--restore-backup 必须与 --sandbox-review 一起使用")
    if args.sandbox != "docker" and args.sandbox_writeback == "on-success":
        parser.error(
            "on-success 仅适用于 Docker；Windows native 使用逐调用回写，其余 local/native 直接修改"
        )
    if args.sandbox_writeback is None:
        try:
            args.sandbox_writeback = (
                writeback_mode(os.getenv("AGENT_SANDBOX_WRITEBACK") or "manual")
                if args.sandbox == "docker"
                else "manual"
            )
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
    if (
        args.project_python
        and not backend_capabilities(args.sandbox, platform=sys.platform).project_python
    ):
        parser.error("--project-python 仅用于 native 模式")
    capabilities = backend_capabilities(args.sandbox, platform=sys.platform)
    if (
        args.sandbox_profile in {"cuda", "metal"}
        and args.sandbox_profile not in capabilities.gpu_profiles
    ):
        parser.error(
            "cuda 仅适用于 Docker / Linux native；metal 仅适用于 Apple Silicon macOS native"
        )
    if args.sandbox_gpus is not None and "cuda" not in capabilities.gpu_profiles:
        parser.error("--sandbox-gpus 仅适用于 NVIDIA CUDA；Metal 使用系统默认设备")
    if args.sandbox_profile == "standard" and args.sandbox_gpus is not None:
        parser.error("GPU selection requires the cuda profile")
    if args.sandbox_gpus is not None:
        try:
            SandboxPolicy(gpus=args.sandbox_gpus)
        except ValueError as error:
            parser.error(str(error))
    return capabilities
