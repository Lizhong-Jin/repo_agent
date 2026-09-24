"""Literal user/project configuration, shared by installed and module entry points."""

import os
import re
from contextlib import contextmanager
from pathlib import Path

from llm.providers import PROVIDERS

from .config_storage import config_lock, read_bytes, replace_config
from .installation import user_config_path as user_config_path

CONFIG_KEYS = frozenset(
    "LLM_PROVIDER LLM_MODEL LLM_BASE_URL LLM_TEMPERATURE LLM_TOOL_CHOICE LLM_THINKING "
    "LLM_THINKING_HISTORY LLM_THINKING_PROFILE LLM_THINKING_RECALL "
    "LLM_REASONING_EFFORT LLM_THINKING_BUDGET LLM_TIMEOUT LLM_STREAM LLM_CONNECT_TIMEOUT "
    "LLM_WRITE_TIMEOUT LLM_POOL_TIMEOUT LLM_CONTEXT_WINDOW LLM_MAX_RETRIES LLM_RETRY_DELAY "
    "LLM_MAX_RETRY_DELAY LLM_EXTRA_JSON AGENT_MAX_STEPS AGENT_MAX_OUTPUT_TOKENS "
    "AGENT_MAX_RECOVERIES AGENT_RECOVERY_MAX_OUTPUT_TOKENS AGENT_THINKING_DISPLAY "
    "AGENT_SYSTEM_PROMPT AGENT_LOG_DIR AGENT_SANDBOX_WRITEBACK "
    "AGENT_SANDBOX_VERIFY_COMMAND AGENT_WEB_SEARCH_PROVIDER BRAVE_SEARCH_API_KEY "
    "AGENT_WEB_FETCH_ENABLED AGENT_AUTO_COMPACT AGENT_COMPACT_THRESHOLD "
    "AGENT_COMPACT_TARGET AGENT_COMPACT_KEEP_TOKENS AGENT_COMPACT_SUMMARY_TOKENS".split()
) | {provider.api_key_env for provider in PROVIDERS.values()}


def read_config(path: Path) -> dict[str, str]:
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match is None:
            raise ValueError(f"{path} 第 {number} 行格式错误，应为 KEY=VALUE")
        key, value = match.groups()
        if key not in CONFIG_KEYS:
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"{path} 第 {number} 行引号不匹配")
            value = value[1:-1]
        if "\x00" in value:
            raise ValueError(f"{path} 第 {number} 行包含不支持的字符")
        values[key] = value
    return values


def load_configuration(root: Path, *, user_only=False):
    """Return literal merged values, their sources, and the selected project config path."""
    path = user_config_path()
    values = read_config(path) if path.exists() else {}
    sources = dict.fromkeys(values, "用户配置")
    if user_only:
        return values, sources, None
    custom = os.environ.get("AGENT_ENV_FILE")
    project_path = Path(custom).expanduser().resolve() if custom else root / ".env"
    if custom or project_path.exists():
        project = read_config(project_path)
        values.update(project)
        sources.update(dict.fromkeys(project, "项目配置"))
    for key in CONFIG_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
            sources[key] = "环境变量"
    return values, sources, project_path


@contextmanager
def configured_environment(root: Path):
    """CLI > nonempty process environment > project (including empty) > user."""
    values, _, project_path = load_configuration(root)
    changes = {key: value for key, value in values.items() if not os.environ.get(key)}
    # Keep a custom project configuration protected by the file tools.
    changes["AGENT_ENV_FILE"] = str(project_path)
    previous = {key: os.environ.get(key) for key in changes}
    try:
        os.environ.update(changes)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def save_user_config(updates: dict[str, str], *, unset=()) -> Path:
    path = user_config_path()
    with config_lock(path):
        return _save_user_config(updates, unset=unset)


def _save_user_config(updates: dict[str, str], *, unset=()) -> Path:
    """Atomically update selected literal values, preserving comments and other settings."""
    if any(key not in CONFIG_KEYS for key in (*updates, *unset)):
        raise ValueError("不能保存不支持的配置项")
    if any(any(ord(char) < 32 or ord(char) == 127 for char in value) for value in updates.values()):
        raise ValueError("配置值必须为单行文本，不能包含控制字符")
    path = user_config_path()
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("用户配置必须是普通文件")
    before = read_bytes(path)
    if before is not None:
        read_config(path)  # Never hide a malformed existing configuration by rewriting it.
    lines = []
    remaining = dict(updates)
    for line in before.decode("utf-8").splitlines() if before is not None else []:
        match = re.fullmatch(r"\s*(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match and match[1] in unset:
            continue
        if match and match[1] in updates:
            key = match[1]
            if key in remaining:
                lines.append(f'{key}="{remaining.pop(key)}"')
        else:
            lines.append(line)
    lines.extend(f'{key}="{value}"' for key, value in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    replace_config(path, ("\n".join(lines) + "\n").encode(), before)
    return path
