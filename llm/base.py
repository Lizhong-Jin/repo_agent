from typing import Protocol

from .schemas import LLMRequest, LLMResponse


class LLM(Protocol):
    """The interface consumed by the future agent runtime."""

    def generate(self, request: LLMRequest) -> LLMResponse: ...


class AsyncLLM(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
