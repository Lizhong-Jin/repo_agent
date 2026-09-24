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
from .model_catalog import ModelInfo, model_info, supported_models, supported_providers
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
