"""Project configuration scope and initial model setup, before resources are opened."""

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from configuration.environment import configured_environment
from host_support.paths import user_config_path
from llm import ConfigurationError, LLMClient, LLMConfig, LLMError
from llm.providers import get_provider

from .models import ModelWizard, persist_selection, prompt_model


@contextmanager
def startup_environment(argv):
    # Resolve --root before reading config; never change the caller's directory.
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument("--root", default=".")
    bootstrap.add_argument("--sandbox-review")
    bootstrap.add_argument("--help", "-h", action="store_true")
    early, _ = bootstrap.parse_known_args(argv)
    if early.help or early.sandbox_review:
        yield
        return
    try:
        with configured_environment(Path(early.root).resolve()):
            yield
    except (OSError, ValueError) as error:
        bootstrap.exit(1, f"配置加载失败：{error}\n")


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


def configuration_hint(args):
    return (
        f"用户配置：{user_config_path()}；"
        f"项目配置：{os.getenv('AGENT_ENV_FILE') or Path(args.root).resolve() / '.env'}"
    )


def prepare_model(parser, args, config_hint):
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
