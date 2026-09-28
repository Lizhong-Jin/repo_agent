"""Shared model and task helpers for conversation tests."""

from dataclasses import replace

from llm import LLMConfig, LLMResponse, Message, Usage
from llm.schemas import ProviderState


class Model:
    def __init__(self, config=None):
        self.config = config or LLMConfig("deepseek", "m", api_key="SYNTHETIC_KEY")
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        message = Message("assistant", "记住了：蓝色")
        message = replace(
            message,
            provider_state=ProviderState(
                self.config.provider,
                self.config.model,
                {"content": message.content, "reasoning_content": "visible thought"},
                message.fingerprint(),
            ),
        )
        return LLMResponse(
            provider=self.config.provider,
            model=self.config.model,
            message=message,
            finish_reason="stop",
            usage=Usage(100, 10),
        )


def perform(conversation, text):
    conversation.start_task(text)
    result = conversation.runtime.run(text, history=conversation.history)
    conversation.finish_task(result)
    return result
