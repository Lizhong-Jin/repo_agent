"""Literal user/project configuration, shared by installed and module entry points."""

import os
import re
from contextlib import contextmanager
from pathlib import Path

from llm.providers import PROVIDERS

CONFIG_KEYS = frozenset(
    "LLM_PROVIDER LLM_MODEL LLM_BASE_URL LLM_TEMPERATURE LLM_TOOL_CHOICE LLM_THINKING "
    "LLM_REASONING_EFFORT LLM_THINKING_BUDGET LLM_TIMEOUT LLM_STREAM LLM_CONNECT_TIMEOUT "
    "LLM_WRITE_TIMEOUT LLM_POOL_TIMEOUT LLM_CONTEXT_WINDOW LLM_MAX_RETRIES LLM_RETRY_DELAY "
    "LLM_MAX_RETRY_DELAY LLM_EXTRA_JSON AGENT_MAX_STEPS AGENT_MAX_OUTPUT_TOKENS "
    "AGENT_SYSTEM_PROMPT AGENT_LOG_DIR AGENT_SANDBOX_WRITEBACK "
    "AGENT_SANDBOX_VERIFY_COMMAND".split()
) | {provider.api_key_env for provider in PROVIDERS.values()}


def user_config_path() -> Path:
    directory = os.environ.get("AGENT_CONFIG_DIR")
    if directory:
        return Path(directory).expanduser().resolve() / ".env"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base.expanduser().resolve() / "repo-agent" / ".env"


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


@contextmanager
def configured_environment(root: Path):
    """CLI > nonempty process environment > project (including empty) > user."""
    global_path = user_config_path()
    values = read_config(global_path) if global_path.exists() else {}
    custom = os.environ.get("AGENT_ENV_FILE")
    project_path = Path(custom).expanduser().resolve() if custom else root / ".env"
    if custom or project_path.exists():
        values.update(read_config(project_path))
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
