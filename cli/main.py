"""Run one task, or start an interactive session when no task argument is supplied."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from agent import AgentRuntime
from agent.skills import SkillRegistry
from agent.Tracing import Tracer
from llm import LLMClient, LLMConfig, LLMError
from sandbox import SandboxPolicy, SandboxSession
from sandbox.environment import DEFAULT_IMAGE, check_image_profile, detect_environment
from tools import create_default_tools

from .config import configured_environment
from .interactive import display_result, finish_writeback, run_interactive
from .live import LiveOutput, SessionStatus, ThinkingControl
from .settings import add_runtime_arguments, request_options, verification_command, writeback_mode


def main() -> None:
    # Resolve --root before reading config; never change the caller's directory.
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--root", default=".")
    bootstrap.add_argument("--sandbox-review")
    bootstrap.add_argument("--help", "-h", action="store_true")
    early, _ = bootstrap.parse_known_args()
    if early.help or early.sandbox_review:
        _main()
        return
    try:
        with configured_environment(Path(early.root).resolve()):
            _main()
    except (OSError, ValueError) as error:
        bootstrap.exit(1, f"配置加载失败：{error}\n")


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="运行可读写项目文件的 Coding Agent", allow_abbrev=False
    )
    parser.add_argument("task", nargs="?", help="需要完成的任务；省略时进入交互模式")
    parser.add_argument("--root", default=".", help="项目根目录，默认当前目录")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "deepseek"))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL"))
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL") or None)
    parser.add_argument("--sandbox", choices=["docker", "local"], default="docker")
    parser.add_argument("--sandbox-image", help=f"自定义沙箱镜像，默认 {DEFAULT_IMAGE}")
    parser.add_argument("--sandbox-profile", choices=["auto", "standard", "cuda"], default="auto")
    parser.add_argument(
        "--sandbox-gpus", help="cuda profile 使用的 GPU：all、单个设备索引或 GPU UUID"
    )
    parser.add_argument("--sandbox-review", help="查看之前保留的 sandbox 会话目录")
    parser.add_argument("--apply", action="store_true", help="与 --sandbox-review 一起显式回写")
    parser.add_argument(
        "--sandbox-writeback",
        type=writeback_mode,
        default=os.getenv("AGENT_SANDBOX_WRITEBACK") or "manual",
        help="回写策略；配置项 AGENT_SANDBOX_WRITEBACK，默认 manual",
    )
    parser.add_argument(
        "--sandbox-verify-command",
        type=verification_command,
        default=os.getenv("AGENT_SANDBOX_VERIFY_COMMAND") or "",
        help="on-success 回写前在容器执行的 JSON 命令数组；配置项 AGENT_SANDBOX_VERIFY_COMMAND",
    )
    parser.add_argument("--restore-backup", help="与 --sandbox-review 一起使用，恢复备份 ID")
    add_runtime_arguments(parser)
    args = parser.parse_args()
    if args.sandbox_review:
        try:
            session = SandboxSession.review(args.sandbox_review)
            if args.apply and args.restore_backup:
                parser.error("--apply 与 --restore-backup 不能同时使用")
            if args.restore_backup:
                print(f"已恢复 {len(session.restore(args.restore_backup))} 个文件。")
                return
            print(session.diff())
            if args.apply:
                print(f"已回写 {len(session.apply())} 个文件。")
                if session.last_backup:
                    print(f"回写前备份：{session.last_backup}")
        except (ValueError, OSError) as error:
            parser.exit(1, f"{error}\n")
        return
    if args.apply:
        parser.error("--apply 必须与 --sandbox-review 一起使用")
    if args.restore_backup:
        parser.error("--restore-backup 必须与 --sandbox-review 一起使用")
    if args.sandbox == "local" and args.sandbox_writeback == "on-success":
        parser.error("on-success 仅适用于 Docker 副本模式，local 会直接修改原项目")
    if args.sandbox == "local" and (
        args.sandbox_profile not in {"auto", "standard"} or args.sandbox_gpus
    ):
        parser.error("GPU profile 仅适用于 Docker 模式")
    if args.sandbox_profile == "standard" and args.sandbox_gpus:
        parser.error("GPU selection requires the cuda profile")
    if args.sandbox_gpus:
        try:
            SandboxPolicy.for_profile("cuda", gpus=args.sandbox_gpus)
        except ValueError as error:
            parser.error(str(error))
    if not args.model:
        parser.error("请通过 --model 或 LLM_MODEL 指定模型 ID")

    tracer = None
    session = None
    try:
        workspace_root = Path(args.root).resolve(strict=True)
        if args.sandbox == "docker":
            environment = detect_environment(
                profile=args.sandbox_profile, image=args.sandbox_image or DEFAULT_IMAGE
            )
            sandbox_policy = SandboxPolicy.for_profile(
                environment.profile, image=args.sandbox_image, gpus=args.sandbox_gpus
            )
            check_image_profile(
                sandbox_policy.image,
                environment.profile,
                allow_unlabelled=args.sandbox_image is not None,
            )
            print(f"[沙箱环境：{environment.profile}] {environment.reason}", flush=True)
            session = SandboxSession(
                workspace_root,
                sandbox_policy,
                verify_command=args.sandbox_verify_command,
            )
            tools = session.tools()
        else:
            tools = create_default_tools(workspace_root)
        skills = SkillRegistry(session.workspace if session is not None else workspace_root)
        extra = request_options(args)
        config = LLMConfig(
            args.provider,
            args.model,
            base_url=(args.base_url.strip() or None) if args.base_url is not None else None,
            timeout=args.timeout,
            stream=args.stream,
            connect_timeout=args.connect_timeout,
            write_timeout=args.write_timeout,
            pool_timeout=args.pool_timeout,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
            max_retry_delay=args.max_retry_delay,
        )
        runtime_options = {"system_prompt": args.system_prompt} if args.system_prompt else {}
        log_dir = os.getenv("AGENT_LOG_DIR") or workspace_root / "logs"
        tracer = Tracer(
            log_dir,
            session_id=os.getenv("AGENT_SESSION_ID") or None,
            provider=args.provider,
            model=args.model,
            workspace=str(workspace_root),
        )
        status = SessionStatus(workspace_root, context_window=args.context_window)

        def on_event(event, stats):
            status(event, stats)
            tracer(event, stats)
            if event == "skill_loaded" and (
                args.task is not None or not (sys.stdin.isatty() and sys.stdout.isatty())
            ):
                print(f"[已加载技能：{stats.skill_loads[-1]['name']}]", flush=True)

        with LLMClient(config) as client, tracer:
            runtime = AgentRuntime(
                client,
                tools=tools,
                max_steps=args.max_steps,
                max_output_tokens=args.max_output_tokens,
                temperature=args.temperature,
                tool_choice=args.tool_choice,
                request_extra=extra,
                on_event=on_event,
                skills=skills,
                **runtime_options,
            )
            runtime.on_model_event = LiveOutput()
            thinking = ThinkingControl(runtime, args)
            if args.task is None:
                if session is None:
                    run_interactive(runtime, thinking=thinking, status=status)
                else:
                    run_interactive(
                        runtime,
                        sandbox=session,
                        writeback=args.sandbox_writeback,
                        thinking=thinking,
                        status=status,
                    )
                return
            if session is not None and args.sandbox_writeback == "on-success":
                session.begin_task()
            print(thinking.describe())
            result = runtime.run(args.task)
            display_result(result)
            print(status.describe())
            writeback_ok = finish_writeback(session, result, args.sandbox_writeback)
    except (LLMError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")
    finally:
        if session is not None:
            print(
                f"Sandbox 副本已保留：{session.directory}\n"
                f"查看：repo-agent --sandbox-review {session.directory}\n"
                "回写：在查看命令后加 --apply"
            )
        if tracer is not None and tracer.error is not None:
            print("追踪日志写入失败，请检查日志目录权限和磁盘空间。", file=sys.stderr)

    if result.status != "completed" or not writeback_ok:
        parser.exit(1)


if __name__ == "__main__":
    main()
