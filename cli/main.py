"""Run one task, or start an interactive session when no task argument is supplied."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from agent import AgentRuntime
from agent.compaction import CompactionSettings
from agent.history import HistoryArchive, HistoryTool
from agent.session import SessionStore
from agent.skills import SkillRegistry
from agent.Tracing import Tracer
from llm import ConfigurationError, LLMClient, LLMConfig, LLMError
from llm.providers import get_provider
from sandbox import SandboxPolicy, SandboxSession
from sandbox.environment import DEFAULT_IMAGE, check_image_profile, detect_environment
from sandbox.native import NativeBackend
from tools import create_default_tools
from tools._internal.web_backend import WebBackend
from tools.tool_groups import DEFAULT_TOOL_GROUPS
from tools.web_tools import create_web_tools

from .config import configured_environment
from .dependencies import available_mode
from .installation import user_config_path
from .interactive import display_result, finish_writeback, run_interactive
from .live import LiveOutput, SessionStatus, ThinkingControl
from .models import ModelControl, ModelWizard, persist_selection, prompt_model
from .paths import installation_root, version_info
from .session import SavedConversation
from .settings import add_runtime_arguments, request_options, verification_command, writeback_mode
from .thinking_display import ThinkingDisplay
from .thinking_store import restore_thinking_args


def model_config(args):
    return LLMConfig(
        args.provider,
        args.model,
        api_key=getattr(args, "api_key", None),
        base_url=(args.base_url.strip() or None) if args.base_url is not None else None,
        timeout=args.timeout,
        stream=args.stream,
        include_thinking=True,
        connect_timeout=args.connect_timeout,
        write_timeout=args.write_timeout,
        pool_timeout=args.pool_timeout,
        max_retries=args.max_retries,
        retry_delay=args.retry_delay,
        max_retry_delay=args.max_retry_delay,
    )


def main() -> None:
    if sys.argv[1:2] in (["version"], ["--version"]):
        import json

        print(json.dumps(version_info(), ensure_ascii=False, indent=2))
        return
    if sys.argv[1:2] == ["uninstall"]:
        from .uninstall import main as uninstall_main

        uninstall_main(sys.argv[2:])
        return
    # Session queries also accept the common --root PATH sessions ... form.
    if sys.argv[1:2] == ["--root"] and sys.argv[3:4] == ["sessions"]:
        from .sessions_command import main as sessions_main

        sessions_main(["--root", sys.argv[2], *sys.argv[4:]])
        return
    if sys.argv[1:2] == ["sessions"]:
        from .sessions_command import main as sessions_main

        sessions_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["toolchains"]:
        from .toolchains import main as toolchains_main

        toolchains_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["doctor"]:
        from .doctor import main as doctor_main

        doctor_main(sys.argv[2:])
        return
    if sys.argv[1:2] == ["config"]:
        from .config_command import main as config_main

        config_main(sys.argv[2:])
        return
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
        description="运行可读写项目文件的 Coding Agent",
        epilog=(
            f"未指定的模型和运行参数自动从配置读取。用户配置：{user_config_path()}。"
            "使用 repo-agent config show 查看配置，config edit 修改配置；toolchains list 查看语言服务；version 查看版本和安装位置，uninstall 卸载当前安装。"
        ),
        allow_abbrev=False,
    )
    parser.add_argument("task", nargs="?", help="需要完成的任务；省略时进入交互模式")
    parser.add_argument(
        "--new-session", action="store_true", help="启动全新会话；默认恢复当前项目上次会话"
    )
    parser.add_argument("--name", help="新会话名称；省略时使用项目内递增序号")
    parser.add_argument("--root", default=".", help="项目根目录，默认当前目录")
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
        help="执行方式：默认沿用安装模式（未登记为 native）；native 原生隔离直接修改；local 无命令执行；docker 使用副本",
    )
    parser.add_argument("--sandbox-image", help=f"自定义沙箱镜像，默认 {DEFAULT_IMAGE}")
    parser.add_argument(
        "--sandbox-profile",
        choices=["auto", "standard", "cuda"],
        default="auto",
        help="默认 auto：Docker / Linux native 自动检测 NVIDIA GPU；standard 禁用，cuda 强制启用",
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
    args = parser.parse_args()
    if args.compact_summary_tokens is not None or os.getenv("AGENT_COMPACT_SUMMARY_TOKENS"):
        print("提示：compact-summary-tokens 已弃用并忽略；压缩大小由 compact-target 指导。")
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
    if args.sandbox != "docker" and args.sandbox_writeback == "on-success":
        parser.error("on-success 仅适用于 Docker 副本模式；local/native 会直接修改原项目")
    if args.sandbox_writeback is None:
        try:
            args.sandbox_writeback = (
                writeback_mode(os.getenv("AGENT_SANDBOX_WRITEBACK") or "manual")
                if args.sandbox == "docker"
                else "manual"
            )
        except argparse.ArgumentTypeError as error:
            parser.error(str(error))
    if args.project_python and args.sandbox != "native":
        parser.error("--project-python 仅用于 native 模式")
    gpu_requested = args.sandbox_profile == "cuda" or args.sandbox_gpus is not None
    if gpu_requested and not (
        args.sandbox == "docker" or (args.sandbox == "native" and sys.platform == "linux")
    ):
        parser.error("GPU profile 仅适用于 Docker 或 Linux / WSL2 native 模式")
    if args.sandbox_profile == "standard" and args.sandbox_gpus is not None:
        parser.error("GPU selection requires the cuda profile")
    if args.sandbox_gpus is not None:
        try:
            SandboxPolicy(gpus=args.sandbox_gpus)
        except ValueError as error:
            parser.error(str(error))
    config_hint = (
        f"用户配置：{user_config_path()}；"
        f"项目配置：{os.getenv('AGENT_ENV_FILE') or Path(args.root).resolve() / '.env'}"
    )
    args.api_key = None
    try:
        key_missing = not (os.getenv(get_provider(args.provider).api_key_env) or "").strip()
    except ConfigurationError:
        key_missing = True
    needs_model = not args.model or not args.model.strip() or key_missing
    if args.configure_model or (needs_model and sys.stdin.isatty() and sys.stdout.isatty()):
        try:
            selection = prompt_model(ModelWizard(args.provider, args.model, base_url=args.base_url))
            args.provider, args.model = selection.provider, selection.model
            args.api_key, args.base_url = selection.api_key, selection.base_url
            args.thinking, args.reasoning_effort, args.thinking_budget = "auto", None, None
            args.extra_json, args.context_window = "{}", None
            args.thinking_history, args.thinking_profile = "auto", "{}"
            args.thinking_explicit = False
            with LLMClient(model_config(args)):
                path = persist_selection(selection, reset=True)
            print(f"模型设置已保存：{path}；{args.provider} / {args.model}")
        except (KeyboardInterrupt, EOFError):
            parser.exit(0, "模型设置已取消，未保存。\n")
        except (LLMError, OSError, ValueError) as error:
            parser.exit(1, f"模型设置未保存：{error}\n{config_hint}\n")
    if not args.model or not args.model.strip():
        parser.error(
            f"尚未配置模型。请在配置文件中填写 LLM_MODEL，或通过 --model 指定。\n{config_hint}"
        )

    tracer = None
    session = None
    models = None
    store = None
    conversation = None
    native = None
    web_backend = None
    try:
        workspace_root = Path(args.root).resolve(strict=True)
        web_backend = WebBackend.from_environment()
        store = SessionStore(workspace_root, new=args.new_session, name=args.name).open()
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
            previous_sandbox = store.data.get("sandbox") if store.data else None
            if previous_sandbox and store.data["mode"] == "docker":
                if store.data.get("sandbox_healthy") is False:
                    raise ValueError("上次沙箱清理未确认；请检查遗留容器后用 --new-session 启动")
                session = SandboxSession.resume(
                    previous_sandbox,
                    workspace_root,
                    sandbox_policy,
                    verify_command=args.sandbox_verify_command,
                )
            else:
                session = SandboxSession(
                    workspace_root,
                    sandbox_policy,
                    verify_command=args.sandbox_verify_command,
                )
            tools = session.tools(writeback_mode=args.sandbox_writeback)
        elif args.sandbox == "native":
            if (
                store.data
                and store.data["mode"] == "native"
                and store.data.get("sandbox_healthy") is False
            ):
                raise ValueError("上次原生进程清理未确认；请检查遗留进程后用 --new-session 启动")
            native = NativeBackend(
                workspace_root, profile=args.sandbox_profile, gpus=args.sandbox_gpus,
                project_python=args.project_python
            )
            tools = native.tools()
            chosen = native.execution_context().get('python_environments', {})
            if chosen:
                print(f"项目 Python：{chosen['project']}（{chosen['source']}）")
            platform_label = "Linux" if sys.platform == "linux" else "macOS"
            print(
                f"[执行环境：{platform_label} native] 工具断网；直接修改原项目，无副本回写",
                flush=True,
            )
            if sys.platform == "linux":
                gpu = native.execution_context()["gpu_access"]
                if gpu["enabled"]:
                    print(
                        f"[原生 GPU：{gpu['selection']}] CUDA kernel 自检通过；无显存配额",
                        flush=True,
                    )
                else:
                    reason = (
                        "已显式关闭 GPU"
                        if args.sandbox_profile == "standard"
                        else "未检测到 NVIDIA CUDA 设备"
                    )
                    print(f"[原生环境：standard] {reason}", flush=True)
        else:
            tools = create_default_tools(workspace_root)
            print("[执行环境：local] 直接修改原项目；不执行命令、Python 或语言服务器", flush=True)
        archive = HistoryArchive(store)
        tools += [HistoryTool(archive, "search"), HistoryTool(archive, "read")]
        tools += create_web_tools(web_backend)
        if web_backend is not None and web_backend.adapter is not None:
            print("[Web 搜索：Brave] 主进程联网搜索；查询会发送给搜索供应商", flush=True)
        if web_backend is not None and web_backend.pages is not None:
            print("[Web 读取] 主进程读取公开网页；不需要搜索密钥", flush=True)
        skills = SkillRegistry(session.workspace if session is not None else workspace_root)
        restore_thinking_args(args)
        extra = request_options(args)
        config = model_config(args)
        runtime_options = {"system_prompt": args.system_prompt} if args.system_prompt else {}
        log_dir = os.getenv("AGENT_LOG_DIR") or workspace_root / "logs"
        tracer = Tracer(
            log_dir,
            provider=args.provider,
            model=args.model,
            workspace=str(workspace_root),
            session_store=store,
        )
        status = SessionStatus(workspace_root, context_window=args.context_window)

        def on_event(event, stats):
            status(event, stats)
            tracer(event, stats)
            if event.startswith("compaction_") and (
                args.task is not None or not (sys.stdin.isatty() and sys.stdout.isatty())
            ):
                print(f"[{stats.compaction['phase']}]", flush=True)
            if event == "recovery" and (
                args.task is not None or not (sys.stdin.isatty() and sys.stdout.isatty())
            ):
                print(f"[{stats.recoveries[-1]['message']}]", flush=True)
            if event == "skill_loaded" and (
                args.task is not None or not (sys.stdin.isatty() and sys.stdout.isatty())
            ):
                print(f"[已加载技能：{stats.skill_loads[-1]['name']}]", flush=True)

        with LLMClient(config) as client, tracer:
            status.bind_context_model(client)
            runtime = AgentRuntime(
                client,
                tools=tools,
                tool_groups=DEFAULT_TOOL_GROUPS,
                max_steps=args.max_steps,
                max_output_tokens=args.max_output_tokens,
                max_recoveries=args.max_recoveries,
                recovery_max_output_tokens=args.recovery_max_output_tokens,
                temperature=args.temperature,
                tool_choice=args.tool_choice,
                request_extra=extra,
                on_event=on_event,
                skills=skills,
                **runtime_options,
            )
            display = ThinkingDisplay(args.thinking_display)
            runtime.on_model_event = LiveOutput(display=display)
            thinking = ThinkingControl(runtime, args)
            conversation = SavedConversation(
                store,
                runtime,
                config,
                status,
                sandbox=session,
                restore_window=args.context_window is None,
                tracer=tracer,
                execution_mode=args.sandbox,
                execution_backend=native,
                compaction_settings=CompactionSettings(
                    auto=args.auto_compact, threshold=args.compact_threshold,
                    target=args.compact_target, keep_tokens=args.compact_keep_tokens,
                    max_refinements=args.compact_max_refinements,
                ),
            )
            conversation.checkpoint(strict=True)
            print(conversation.notice)
            if args.task is None:
                models = ModelControl(
                    runtime,
                    config,
                    thinking=thinking,
                    status=status,
                    tracer=tracer,
                    client_factory=LLMClient,
                )
                if session is None:
                    run_interactive(
                        runtime,
                        thinking=thinking,
                        status=status,
                        models=models,
                        display=display,
                        conversation=conversation,
                    )
                else:
                    run_interactive(
                        runtime,
                        sandbox=session,
                        writeback=args.sandbox_writeback,
                        thinking=thinking,
                        status=status,
                        models=models,
                        display=display,
                        conversation=conversation,
                    )
                return
            if session is not None and args.sandbox_writeback == "on-success":
                session.begin_task()
            print(thinking.describe())
            conversation.start_task(args.task)
            result = runtime.run(args.task, history=conversation.history)
            display_result(result)
            print(status.describe())
            writeback_ok = finish_writeback(session, result, args.sandbox_writeback)
            conversation.finish_task(result)
    except ConfigurationError as error:
        parser.exit(1, f"模型配置不完整或无效：{error}\n{config_hint}\n")
    except (LLMError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")
    finally:
        if web_backend is not None:
            web_backend.close()
        saved_ok = conversation.checkpoint() if conversation is not None else True
        if native is not None:
            native.close()
        if store is not None:
            store.close()
        if models is not None:
            models.close()
        if session is not None:
            print(
                f"Sandbox 副本已保留：{session.directory}\n"
                f"查看：repo-agent --sandbox-review {session.directory}\n"
                "回写：在查看命令后加 --apply"
            )
        if tracer is not None and (
            tracer.error is not None or getattr(tracer, "previous_error", None)
        ):
            print("追踪日志写入失败，请检查日志目录权限和磁盘空间。", file=sys.stderr)
        if not saved_ok:
            parser.exit(1, "会话未能保存，请检查状态目录权限和磁盘空间。\n")

    if result.status != "completed" or not writeback_ok:
        parser.exit(1)


if __name__ == "__main__":
    main()
