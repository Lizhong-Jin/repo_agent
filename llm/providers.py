"""Endpoint presets, not a hard-coded model catalogue. Models are selected by the caller."""

from dataclasses import dataclass
from typing import Literal

from .errors import ConfigurationError

APIFormat = Literal["chat_completions", "responses", "anthropic", "gemini"]


@dataclass(frozen=True)
class Provider:
    name: str
    api_format: APIFormat
    base_url: str
    api_key_env: str


PROVIDERS = {
    "openai": Provider("openai", "responses", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": Provider(
        "anthropic", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"
    ),
    "gemini": Provider(
        "gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY"
    ),
    "deepseek": Provider(
        "deepseek", "chat_completions", "https://api.deepseek.com", "DEEPSEEK_API_KEY"
    ),
    "qwen": Provider(
        "qwen",
        "chat_completions",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
    ),
    "moonshot": Provider(
        "moonshot", "chat_completions", "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY"
    ),
    "zhipu": Provider(
        "zhipu", "chat_completions", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"
    ),
    "doubao": Provider(
        "doubao", "chat_completions", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY"
    ),
    "minimax": Provider(
        "minimax", "chat_completions", "https://api.minimax.cn/v1", "MINIMAX_API_KEY"
    ),
}
ALIASES = {"chatgpt": "openai", "claude": "anthropic", "kimi": "moonshot", "glm": "zhipu"}


def get_provider(name: str) -> Provider:
    if not isinstance(name, str):
        raise ConfigurationError("provider must be a string")
    key = ALIASES.get(name.lower(), name.lower())
    try:
        return PROVIDERS[key]
    except KeyError:
        raise ConfigurationError(f"Unknown provider; choose from: {', '.join(PROVIDERS)}") from None
