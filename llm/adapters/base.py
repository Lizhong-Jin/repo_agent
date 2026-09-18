"""Pure payload conversion; HTTP and credentials stay in the client."""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import replace
from typing import Any

from ..errors import InvalidRequestError, InvalidResponseError
from ..schemas import FinishReason, LLMRequest, LLMResponse, Message, ProviderState, ToolCall, Usage


def count(data: dict, key: str) -> int | None:
    value = data.get(key)
    if value is not None and (type(value) is not int or value < 0):
        raise InvalidResponseError("Provider returned an invalid token counter")
    return value


def usage(input_tokens: int | None, output_tokens: int | None, **kwargs: Any) -> Usage:
    total = kwargs.pop("total_tokens", None)
    if total is None and input_tokens is not None and output_tokens is not None:
        total = input_tokens + output_tokens
    return Usage(input_tokens, output_tokens, total, **kwargs)


def tool_call(call_id: str, name: str, arguments: Any) -> ToolCall:
    import json

    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    return ToolCall(call_id, name, arguments)


class Adapter(ABC):
    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model

    @abstractmethod
    def path(self) -> str: ...

    @abstractmethod
    def encode(self, request: LLMRequest) -> dict: ...

    @abstractmethod
    def decode(self, data: dict) -> LLMResponse: ...

    def state(self, message: Message) -> Any:
        state = message.provider_state
        if state is None:
            return None
        if (state.provider, state.model) != (self.provider, self.model):
            raise InvalidRequestError(
                "Provider state belongs to a different provider/model; start a fresh conversation"
            )
        if state.fingerprint != message.fingerprint():
            raise InvalidRequestError(
                "Assistant message was edited without rebuilding provider state"
            )
        return deepcopy(state.payload)

    def assistant(self, text: str, calls: list[ToolCall], payload: Any) -> Message:
        message = Message("assistant", text, tuple(calls))
        return replace(
            message,
            provider_state=ProviderState(
                self.provider, self.model, deepcopy(payload), message.fingerprint()
            ),
        )

    def extras(self, body: dict, request: LLMRequest) -> dict:
        # An escape hatch for model-specific options, never for bypassing the common contract.
        reserved = {
            "model",
            "messages",
            "input",
            "contents",
            "system",
            "systemInstruction",
            "tools",
            "tool_choice",
            "toolConfig",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "temperature",
            "stream",
            "stream_options",
            "n",
            "candidateCount",
            "store",
            "previous_response_id",
            "conversation",
            "background",
        }
        if reserved.intersection(request.extra):
            raise InvalidRequestError("extra cannot override unified fields or execution mode")
        result = deepcopy(body)
        for key, value in request.extra.items():
            if key == "generationConfig":
                if not isinstance(value, dict):
                    raise InvalidRequestError("generationConfig must be an object")
                if {"maxOutputTokens", "temperature", "candidateCount"}.intersection(value):
                    raise InvalidRequestError("generationConfig cannot override unified fields")
                result.setdefault(key, {}).update(deepcopy(value))
            else:
                result[key] = deepcopy(value)
        return result

    def response(
        self,
        data: dict,
        message: Message,
        reason: FinishReason,
        tokens: Usage,
        native_reason: str | None,
    ) -> LLMResponse:
        if len({call.id for call in message.tool_calls}) != len(message.tool_calls):
            raise InvalidResponseError("Provider returned duplicate tool call IDs")
        if reason == "tool_calls" and not message.tool_calls:
            raise InvalidResponseError("Provider reported tool calls without any calls")
        if reason == "stop":
            if message.tool_calls:
                reason = "tool_calls"
            elif not message.content:
                raise InvalidResponseError("Provider completed without text or tool calls")
        return LLMResponse(
            provider=self.provider,
            model=data.get("model") or data.get("modelVersion") or self.model,
            message=message,
            finish_reason=reason,
            usage=tokens,
            id=data.get("id") or data.get("responseId"),
            provider_finish_reason=native_reason,
            raw=deepcopy(data),
        )
