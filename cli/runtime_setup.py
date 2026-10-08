"""Assemble the runtime and saved conversation, owning the model and trace contexts."""

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass

from agent import AgentRuntime
from agent.compaction import CompactionSettings
from agent.conversation import SavedConversation
from agent.history import HistoryArchive, HistoryTool
from agent.skills import SkillRegistry
from agent.Tracing import Tracer
from llm import LLMClient, LLMConfig
from tools.access_policy import AccessPolicy
from tools.tool_groups import DEFAULT_TOOL_GROUPS
from tools.web_tools import create_web_tools

from .output import LiveOutput
from .review_mode import review_log_directory
from .runtime_events import SessionEvents
from .session_status import SessionStatus
from .settings import request_options
from .startup import model_config
from .thinking_control import ThinkingControl
from .thinking_display import ThinkingDisplay
from .thinking_store import restore_thinking_args


@dataclass
class RuntimeSession:
    runtime: AgentRuntime
    config: LLMConfig
    status: SessionStatus
    thinking: ThinkingControl
    display: ThinkingDisplay
    conversation: SavedConversation
    tracer: Tracer
    client_factory: object


def report_trace_error(tracer):
    if tracer.error is not None or getattr(tracer, "previous_error", None):
        print("追踪日志写入失败，请检查日志目录权限和磁盘空间。", file=sys.stderr)


@contextmanager
def open_runtime(args, workspace_root, store, environment, web_backend):
    session, native = environment.sandbox, environment.native
    policy = AccessPolicy(getattr(args, "mode", None) or "develop")
    tools = list(environment.tools)
    archive = HistoryArchive(store)
    tools += [HistoryTool(archive, "search"), HistoryTool(archive, "read")]
    tools += create_web_tools(web_backend) if not policy.read_only else []
    if not policy.read_only and web_backend is not None and web_backend.adapter is not None:
        print("[Web 搜索：Brave] 主进程联网搜索；查询会发送给搜索供应商", flush=True)
    if not policy.read_only and web_backend is not None and web_backend.pages is not None:
        print("[Web 读取] 主进程读取公开网页；不需要搜索密钥", flush=True)
    skills = SkillRegistry(session.workspace if session is not None else workspace_root)
    restore_thinking_args(args)
    extra = request_options(args)
    config = model_config(args)
    runtime_options = {"system_prompt": args.system_prompt} if args.system_prompt else {}
    log_dir = os.getenv("AGENT_LOG_DIR") or workspace_root / "logs"
    if policy.read_only:
        log_dir = review_log_directory(store, workspace_root)
    tracer = Tracer(
        log_dir,
        provider=args.provider,
        model=args.model,
        workspace=str(workspace_root),
        session_store=store,
    )
    status = SessionStatus(workspace_root, context_window=args.context_window)

    on_event = SessionEvents(
        status,
        tracer,
        show_notices=args.task is not None or not (sys.stdin.isatty() and sys.stdout.isatty()),
    )

    try:
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
                access_policy=policy,
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
                execution_mode="local" if policy.read_only else args.sandbox,
                execution_backend=native,
                compaction_settings=CompactionSettings(
                    auto=args.auto_compact,
                    threshold=args.compact_threshold,
                    target=args.compact_target,
                    keep_tokens=args.compact_keep_tokens,
                    max_refinements=args.compact_max_refinements,
                ),
                workspace=getattr(environment, "workspace", None),
            )
            yield RuntimeSession(
                runtime, config, status, thinking, display, conversation, tracer, LLMClient
            )
    finally:
        report_trace_error(tracer)
