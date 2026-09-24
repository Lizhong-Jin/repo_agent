"""Runtime options read the merged environment; explicit CLI flags override it."""

import argparse
import json
import os
from typing import Any

from agent.compaction import CompactionSettings
from llm import ConfigurationError
from llm.thinking import thinking_options

from .thinking_display import display_mode


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("请输入正整数 token 数") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("请输入正整数 token 数")
    return number


def step_limit(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("轮数上限必须为非负整数；0 表示无上限") from None
    if number < 0:
        raise argparse.ArgumentTypeError("轮数上限必须为非负整数；0 表示无上限")
    return number


def stream_value(value: str) -> bool:
    if value.lower() not in {"true", "false", "1", "0"}:
        raise argparse.ArgumentTypeError("LLM_STREAM must be true, false, 1 or 0")
    return value.lower() in {"true", "1"}


THINKING_FIELDS = {
    "thinking",
    "reasoning",
    "reasoning_effort",
    "enable_thinking",
    "thinking_budget",
    "output_config",
}


def native_thinking(extra):
    return bool(THINKING_FIELDS.intersection(extra)) or (
        isinstance(extra.get("generationConfig"), dict)
        and "thinkingConfig" in extra["generationConfig"]
    )


class ThinkingArgument(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.thinking_explicit = True


_COMPACTION_DEFAULTS = CompactionSettings()
RUNTIME_OPTIONS = (
    ("auto-compact", "AGENT_AUTO_COMPACT", _COMPACTION_DEFAULTS.auto, stream_value),
    ("compact-threshold", "AGENT_COMPACT_THRESHOLD", _COMPACTION_DEFAULTS.threshold, float),
    ("compact-target", "AGENT_COMPACT_TARGET", _COMPACTION_DEFAULTS.target, float),
    ("compact-keep-tokens", "AGENT_COMPACT_KEEP_TOKENS",
     _COMPACTION_DEFAULTS.keep_tokens, positive_int),
    ("compact-max-refinements", "AGENT_COMPACT_MAX_REFINEMENTS",
     _COMPACTION_DEFAULTS.max_refinements, step_limit),
    ("thinking-display", "AGENT_THINKING_DISPLAY", "collapsed", display_mode),
    ("max-steps", "AGENT_MAX_STEPS", 8, step_limit),
    ("max-output-tokens", "AGENT_MAX_OUTPUT_TOKENS", 4096, int),
    ("max-recoveries", "AGENT_MAX_RECOVERIES", 2, int),
    ("recovery-max-output-tokens", "AGENT_RECOVERY_MAX_OUTPUT_TOKENS", None, positive_int),
    ("context-window", "LLM_CONTEXT_WINDOW", None, positive_int),
    ("temperature", "LLM_TEMPERATURE", None, float),
    ("tool-choice", "LLM_TOOL_CHOICE", "auto", str),
    ("thinking", "LLM_THINKING", "auto", str),
    ("reasoning-effort", "LLM_REASONING_EFFORT", None, str),
    ("thinking-budget", "LLM_THINKING_BUDGET", None, int),
    ("thinking-history", "LLM_THINKING_HISTORY", "auto", str),
    ("thinking-profile", "LLM_THINKING_PROFILE", "{}", str),
    ("thinking-recall", "LLM_THINKING_RECALL", True, stream_value),
    ("timeout", "LLM_TIMEOUT", 300.0, float),
    ("connect-timeout", "LLM_CONNECT_TIMEOUT", 10.0, float),
    ("write-timeout", "LLM_WRITE_TIMEOUT", 30.0, float),
    ("pool-timeout", "LLM_POOL_TIMEOUT", 10.0, float),
    ("max-retries", "LLM_MAX_RETRIES", 2, int),
    ("retry-delay", "LLM_RETRY_DELAY", 0.5, float),
    ("max-retry-delay", "LLM_MAX_RETRY_DELAY", 30.0, float),
    ("system-prompt", "AGENT_SYSTEM_PROMPT", None, str),
    ("extra-json", "LLM_EXTRA_JSON", "{}", str),
)


def add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    # Kept only to allow existing startup scripts to migrate without failure.
    parser.add_argument("--compact-summary-tokens", default=None, help=argparse.SUPPRESS)
    for flag, env, default, kind in RUNTIME_OPTIONS:
        parser.add_argument(
            f"--{flag}",
            type=kind,
            action=ThinkingArgument
            if flag
            in {
                "thinking",
                "reasoning-effort",
                "thinking-budget",
                "thinking-history",
                "thinking-profile",
            }
            else "store",
            default=os.getenv(env) or default,
            help=(
                f"配置项 {env}；默认 {default if default is not None else '不指定'}"
                + ("；0 表示轮数无上限" if flag == "max-steps" else "")
            ),
        )

    try:
        stream = stream_value(os.getenv("LLM_STREAM") or "true")
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    parser.add_argument(
        "--stream",
        action=argparse.BooleanOptionalAction,
        default=stream,
        help="启用模型流式响应（默认开启）",
    )


def request_options(args: argparse.Namespace) -> dict[str, Any]:
    try:
        extra = json.loads(args.extra_json)
    except (ValueError, TypeError):
        raise ConfigurationError("LLM_EXTRA_JSON must be a JSON object") from None
    if not isinstance(extra, dict):
        raise ConfigurationError("LLM_EXTRA_JSON must be a JSON object")
    thinking = thinking_options(
        args.provider,
        args.model,
        mode=args.thinking,
        effort=args.reasoning_effort,
        budget=args.thinking_budget,
        max_output_tokens=args.max_output_tokens,
        history=getattr(args, "thinking_history", "auto"),
        base_url=getattr(args, "base_url", None),
        profile=getattr(args, "thinking_profile", "{}"),
    )
    if thinking and native_thinking(extra):
        raise ConfigurationError("LLM_EXTRA_JSON conflicts with explicit thinking settings")
    merged = {**extra, **thinking}
    if "generationConfig" in thinking and "generationConfig" in extra:
        if not isinstance(extra["generationConfig"], dict):
            raise ConfigurationError("generationConfig 必须为对象")
        merged["generationConfig"] = {**extra["generationConfig"], **thinking["generationConfig"]}
    return merged


def writeback_mode(value: str) -> str:
    if value not in {"manual", "on-success"}:
        raise argparse.ArgumentTypeError("回写策略必须为 manual 或 on-success")
    return value


def verification_command(value: str) -> list[str] | None:
    if not value.strip():
        return None
    try:
        command = json.loads(value)
    except ValueError:
        raise argparse.ArgumentTypeError("最终验证命令必须为 JSON 参数数组") from None
    if (
        not isinstance(command, list)
        or not 1 <= len(command) <= 128
        or any(not isinstance(arg, str) or "\x00" in arg or len(arg) > 4096 for arg in command)
        or not command[0]
    ):
        raise argparse.ArgumentTypeError(
            "最终验证命令必须为非空 JSON 字符串数组（最多 128 项，每项最多 4096 字符）"
        )
    return command
