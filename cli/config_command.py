"""Inspect configuration and edit user defaults without starting an agent or Docker."""

import argparse
import getpass
import json
import math
import re
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from llm import ConfigurationError, LLMClient, LLMConfig
from llm.providers import PROVIDERS, get_provider

from .config import CONFIG_KEYS, load_configuration, read_config, save_user_config
from .config_storage import backup_config, backups, reset_config, restore_config
from .installation import user_config_path
from .models import ModelWizard, clean_value, persist_selection, prompt_model
from .settings import (
    RUNTIME_OPTIONS,
    request_options,
    stream_value,
    verification_command,
    writeback_mode,
)

SECRET_KEYS = {provider.api_key_env for provider in PROVIDERS.values()}
BUILTINS = {key: "" if value is None else str(value) for _, key, value, _ in RUNTIME_OPTIONS}
BUILTINS.update(
    {
        "LLM_PROVIDER": "deepseek",
        "LLM_MODEL": "",
        "LLM_BASE_URL": "",
        "LLM_STREAM": "true",
        "AGENT_LOG_DIR": "",
        "AGENT_SANDBOX_WRITEBACK": "manual",
        "AGENT_SANDBOX_VERIFY_COMMAND": "",
    }
)
KEYS = [
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    *sorted(CONFIG_KEYS - {"LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL"}),
]


def safe_value(key, value, values):
    if key in SECRET_KEYS:
        return "[已设置，已隐藏]" if value else "（未设置）"
    # Hide known credentials even if repeated in another setting, e.g. an API URL.
    for secret in sorted((values.get(k, "") for k in SECRET_KEYS), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[已隐藏]")
    return json.dumps(value, ensure_ascii=False) if value else "（空）"


def fallback(key, values, root):
    if key == "LLM_BASE_URL":
        try:
            return get_provider(values.get("LLM_PROVIDER") or "deepseek").base_url
        except ConfigurationError:
            return "（供应商无效，无法确定默认地址）"
    if key == "AGENT_LOG_DIR":
        return str(root / "logs")
    if key == "AGENT_SYSTEM_PROMPT":
        return "（内置系统提示词）"
    return BUILTINS.get(key, "")


def show(root, *, user_only=False):
    values, sources, project = load_configuration(root, user_only=user_only)
    print(f"用户配置：{user_config_path()}")
    if project is not None:
        print(f"项目配置：{project}")
        print("启动生效配置（未包含额外启动参数或已运行会话中的临时设置）：")
    else:
        print("用户文件中保存的配置：")
    for key in KEYS:
        value = values.get(key, "")
        source = sources.get(key, "未写入" if user_only else "内置默认")
        if not user_only and not value:
            value = fallback(key, values, root)
            if key in sources and value:
                source += "（空值，采用默认行为）"
        print(f"{key} = {safe_value(key, value, values)}    [{source}]")
    if not user_only:
        print("优先级：启动参数 > 非空环境变量 > 项目配置 > 用户配置 > 内置默认。")


def validate_value(key, value):
    value = clean_value(value)
    if not value:
        return value
    try:
        if key == "LLM_PROVIDER":
            return get_provider(value).name
        if key in {"LLM_STREAM", "LLM_THINKING_RECALL"}:
            return "true" if stream_value(value) else "false"
        if key == "AGENT_SANDBOX_WRITEBACK":
            return writeback_mode(value)
        if key == "AGENT_SANDBOX_VERIFY_COMMAND":
            verification_command(value)
        elif key == "LLM_THINKING_PROFILE":
            from llm.thinking_profiles import parse_profile

            parse_profile(value)
        elif key == "LLM_THINKING_HISTORY":
            if value not in {"auto", "on", "off"}:
                raise ValueError
        elif key == "LLM_EXTRA_JSON":
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError
            json.dumps(parsed, allow_nan=False)
        elif key in {
            "AGENT_MAX_STEPS",
            "AGENT_MAX_OUTPUT_TOKENS",
            "AGENT_MAX_RECOVERIES",
            "AGENT_RECOVERY_MAX_OUTPUT_TOKENS",
            "LLM_CONTEXT_WINDOW",
            "LLM_THINKING_BUDGET",
            "LLM_MAX_RETRIES",
        }:
            number = int(value)
            minimum = (
                0 if key in {"LLM_MAX_RETRIES", "AGENT_MAX_RECOVERIES", "AGENT_MAX_STEPS"} else 1
            )
            if number < minimum or (key == "LLM_MAX_RETRIES" and number > 10):
                raise ValueError
        elif key in {
            "LLM_TIMEOUT",
            "LLM_CONNECT_TIMEOUT",
            "LLM_WRITE_TIMEOUT",
            "LLM_POOL_TIMEOUT",
            "LLM_RETRY_DELAY",
            "LLM_MAX_RETRY_DELAY",
            "LLM_TEMPERATURE",
        }:
            number = float(value)
            if (
                not math.isfinite(number)
                or number < 0
                or (key.endswith("TIMEOUT") and number == 0)
                or (key == "LLM_TEMPERATURE" and number > 2)
            ):
                raise ValueError
        elif key in {
            "LLM_THINKING",
            "LLM_TOOL_CHOICE",
            "LLM_REASONING_EFFORT",
            "AGENT_THINKING_DISPLAY",
        }:
            allowed = {
                "AGENT_THINKING_DISPLAY": {"collapsed", "expanded", "hidden"},
                "LLM_THINKING": {"auto", "enabled", "disabled", "adaptive"},
                "LLM_TOOL_CHOICE": {"auto", "none", "required"},
                "LLM_REASONING_EFFORT": {"minimal", "low", "medium", "high", "xhigh", "max"},
            }
            if value not in allowed[key]:
                raise ValueError
        elif key == "LLM_BASE_URL":
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError
    except (ValueError, ConfigurationError, argparse.ArgumentTypeError):
        raise ValueError(f"{key} 的值无效，未保存；可参考 .env.example 中的格式") from None
    return value


def validate_values(values):
    for key, value in values.items():
        validate_value(key, value)
    options = {
        flag.replace("-", "_"): kind(values[key]) if values.get(key) else default
        for flag, key, default, kind in RUNTIME_OPTIONS
    }
    ceiling = options["recovery_max_output_tokens"]
    if ceiling is not None and ceiling < options["max_output_tokens"]:
        raise ValueError("AGENT_RECOVERY_MAX_OUTPUT_TOKENS 不能低于 AGENT_MAX_OUTPUT_TOKENS")
    options.update(
        provider=values.get("LLM_PROVIDER") or "deepseek",
        model=values.get("LLM_MODEL") or "",
        base_url=values.get("LLM_BASE_URL") or None,
    )
    try:
        request_options(SimpleNamespace(**options))
    except ConfigurationError as error:
        # Current validators use fixed messages; still redact known credentials.
        message = str(error)
        for key in SECRET_KEYS:
            if values.get(key):
                message = message.replace(values[key], "[已隐藏]")
        raise ValueError(f"配置组合冲突：{message}") from None


def validate_file(path):
    values = read_config(path)
    validate_values(values)
    return values


def validate_configuration(root, *, user_only=False):
    values, _, project = load_configuration(root, user_only=user_only)
    warnings = []
    for path in (user_config_path(), project):
        if path is None or not path.exists():
            continue
        seen = set()
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = re.fullmatch(r"\s*(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
            if match:
                key = match[1]
                if key not in CONFIG_KEYS:
                    warnings.append(f"{path} 第 {number} 行：未知配置项（已忽略）")
                elif key in seen:
                    warnings.append(f"{path} 第 {number} 行：重复配置项（后面的值生效）")
                seen.add(key)
        # Catch invalid saved settings even when an environment override hides them.
        validate_values(read_config(path))
    validate_values(values)
    provider = get_provider(values.get("LLM_PROVIDER") or "deepseek")
    if not values.get("LLM_MODEL", "").strip():
        warnings.append("未设置 LLM_MODEL；可运行 repo-agent config model")
    if not values.get(provider.api_key_env, "").strip():
        warnings.append(f"未设置 {provider.api_key_env}；可运行 repo-agent config model")
    return warnings


def prompt_value(key, values):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("交互编辑需要终端；普通设置也可使用 config set KEY VALUE")
    if key == "LLM_PROVIDER":
        print("可选供应商：" + ", ".join(PROVIDERS))
    print(f"{key} 当前用户值：{safe_value(key, values.get(key, ''), values)}")
    prompt = "新值（回车保留，:empty 清空；Ctrl+C 取消）> "
    if key in SECRET_KEYS:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                answer = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("当前终端无法隐藏 API Key 输入，未修改配置") from None
    else:
        answer = input(prompt)
    return "" if answer == ":empty" else answer if answer else values.get(key, "")


def save(key, value, root):
    path = save_user_config({key: validate_value(key, value)})
    print(f"已更新 {key}，保存到：{path}")
    print("重新启动 Agent 后生效；会话内切换模型请用 /model。")
    try:
        _, sources, _ = load_configuration(root)
        if sources.get(key) in {"项目配置", "环境变量"}:
            print(f"当前 {key} 仍被{sources[key]}覆盖；config show 可查看来源。")
    except (ValueError, OSError):
        print("用户配置已保存；项目配置无法读取，可修复后用 config show 检查。")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="repo-agent config",
        description="查看配置或修改用户默认配置，不启动 Agent、Docker 或模型请求",
    )
    commands = parser.add_subparsers(dest="action")
    display = commands.add_parser("show", help="查看生效值及来源，隐藏 API Key")
    display.add_argument("--user", action="store_true", help="只查看用户文件保存的值")
    display.add_argument("--root", type=Path, default=Path.cwd(), help="计算生效配置的工作目录")
    commands.add_parser("path", help="显示用户配置文件位置")
    edit = commands.add_parser("edit", help="交互选择一个配置项并修改")
    edit.add_argument("key", nargs="?", choices=KEYS)
    edit.add_argument("--root", type=Path, default=Path.cwd())
    setting = commands.add_parser("set", help="保存一个配置项；API Key 省略值后隐藏输入")
    setting.add_argument("key", choices=KEYS)
    setting.add_argument("value", nargs="?")
    setting.add_argument("--root", type=Path, default=Path.cwd())
    commands.add_parser("model", help="选择供应商并设置模型及 API Key，保存后退出")
    validation = commands.add_parser("validate", help="离线校验格式、参数冲突及未知字段")
    validation.add_argument("--user", action="store_true")
    validation.add_argument("--root", type=Path, default=Path.cwd())
    unset = commands.add_parser("unset", help="删除一个用户覆盖值，不影响项目或环境变量")
    unset.add_argument("key", choices=KEYS)
    commands.add_parser("backup", help="备份当前用户配置，格式错误时也可用")
    commands.add_parser("backups", help="列出可恢复的用户配置备份，不显示内容")
    restore = commands.add_parser("restore", help="从指定备份恢复，先备份当前文件")
    restore.add_argument("name", help="config backups 列出的完整备份名")
    commands.add_parser("reset", help="备份当前文件后恢复 .env.example 默认模板，清空模型和 Key")
    args = parser.parse_args(argv)
    try:
        root = getattr(args, "root", Path.cwd()).expanduser().resolve()
        if args.action in {None, "show"}:
            show(root, user_only=getattr(args, "user", False))
        elif args.action == "path":
            print(user_config_path())
        elif args.action == "validate":
            for warning in validate_configuration(root, user_only=args.user):
                print(f"[WARN] {warning}")
            print("配置校验通过；未连接模型服务，未验证 Key 或模型可用性。")
        elif args.action == "unset":
            save_user_config({}, unset=(args.key,))
            print(f"已删除用户配置项 {args.key}；项目配置和环境变量仍可覆盖默认值。")
        elif args.action == "backup":
            print(f"已备份：{backup_config(user_config_path()).name}")
        elif args.action == "backups":
            entries = backups(user_config_path())
            print("\n".join(path.name for path in entries) if entries else "暂无配置备份。")
        elif args.action in {"restore", "reset"}:
            path = user_config_path()
            if args.action == "restore":
                backup = restore_config(path, args.name, validate_file)
            else:
                template = Path(__file__).resolve().parents[1] / ".env.example"
                validate_file(template)
                backup = reset_config(path, template)
            action = (
                "恢复配置"
                if args.action == "restore"
                else "恢复默认模板（模型和 API Key 需重新设置）"
            )
            print(f"已{action}：{path}")
            if backup:
                print(f"原文件已备份：{backup.name}")
            print("重新启动 Agent 后生效；config show 可查看实际生效值。")
        elif args.action == "model":
            values, _, _ = load_configuration(root, user_only=True)
            selection = prompt_model(
                ModelWizard(
                    values.get("LLM_PROVIDER", "deepseek"),
                    values.get("LLM_MODEL"),
                    base_url=values.get("LLM_BASE_URL") or None,
                )
            )
            with LLMClient(
                LLMConfig(
                    selection.provider,
                    selection.model,
                    api_key=selection.api_key,
                    base_url=selection.base_url,
                )
            ):
                path = persist_selection(selection, reset=True)
            print(f"模型设置已保存：{path}；重新启动后生效。")
        else:
            values, _, _ = load_configuration(root, user_only=True)
            key = args.key
            if key is None:
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    raise ValueError("交互编辑需要终端；也可使用 config set KEY VALUE")
                for index, item in enumerate(KEYS, 1):
                    print(f"{index:2}. {item} = {safe_value(item, values.get(item, ''), values)}")
                answer = input("选择序号或配置名（回车取消）> ").strip()
                if not answer:
                    print("已取消，未修改配置。")
                    return
                key = (
                    KEYS[int(answer) - 1]
                    if answer.isdigit() and 1 <= int(answer) <= len(KEYS)
                    else answer
                )
                if key not in CONFIG_KEYS:
                    raise ValueError("请选择列表中的配置项")
            value = getattr(args, "value", None)
            if value is not None and key in SECRET_KEYS:
                raise ValueError("API Key 请省略命令行值，使用 config set KEY 隐藏输入")
            if value is None:
                value = prompt_value(key, values)
            save(key, value, root)
    except (EOFError, KeyboardInterrupt):
        parser.exit(0, "已取消，未修改配置。\n")
    except (ValueError, OSError, ConfigurationError) as error:
        parser.exit(1, f"配置操作未完成：{error}\n")


if __name__ == "__main__":
    main()
