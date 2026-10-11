"""Public API: provider-neutral inputs and outputs for the coding agent."""

from .base import LLM, AsyncLLM
from .errors import (
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    InvalidResponseError,
    LLMConnectionError,
    LLMError,
    LLMTimeoutError,
    ProviderError,
    RateLimitError,
)
from .model_catalog import ModelInfo, model_info, supported_models, supported_providers
from .providers import PROVIDERS, get_provider
from .schemas import LLMRequest, LLMResponse, Message, ToolCall, ToolDefinition, Usage


def __getattr__(name):
    # Schema-only consumers include network-denied sandbox workers. Loading a
    # transport here would eagerly initialize asyncio/httpx on their behalf.
    if name in {"AsyncLLMClient", "LLMClient", "LLMConfig"}:
        from . import client

        value = getattr(client, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "LLM",
    "AsyncLLM",
    "LLMClient",
    "AsyncLLMClient",
    "LLMConfig",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ToolCall",
    "ToolDefinition",
    "Usage",
    "PROVIDERS",
    "get_provider",
    "ModelInfo",
    "model_info",
    "supported_models",
    "supported_providers",
    "LLMError",
    "ConfigurationError",
    "InvalidRequestError",
    "InvalidResponseError",
    "AuthenticationError",
    "RateLimitError",
    "ProviderError",
    "LLMTimeoutError",
    "LLMConnectionError",
]
