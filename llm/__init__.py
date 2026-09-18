"""Public API: provider-neutral inputs and outputs for the coding agent."""

from .base import LLM, AsyncLLM
from .client import AsyncLLMClient, LLMClient, LLMConfig
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
from .providers import PROVIDERS, get_provider
from .schemas import LLMRequest, LLMResponse, Message, ToolCall, ToolDefinition, Usage

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
