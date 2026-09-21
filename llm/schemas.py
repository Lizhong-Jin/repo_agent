"""Provider-neutral text and function-calling contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .errors import InvalidRequestError

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "tool_calls", "length", "blocked", "other"]
ToolChoice = Literal["auto", "none", "required"]


def json_string(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (ValueError, TypeError):
        raise InvalidRequestError(
            "Value must be JSON serializable, without NaN or Infinity"
        ) from None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise InvalidRequestError("ToolCall.id must be a non-empty string")
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.name):
            raise InvalidRequestError("ToolCall.name must use 1-64 letters, digits, '_' or '-'")
        if not isinstance(self.arguments, dict):
            raise InvalidRequestError("ToolCall.arguments must be a JSON object")
        json_string(self.arguments)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def __post_init__(self) -> None:
        ToolCall("validation", self.name, {})
        if not isinstance(self.description, str):
            raise InvalidRequestError("Tool description must be text")
        if not isinstance(self.parameters, dict) or self.parameters.get("type") != "object":
            raise InvalidRequestError("Tool parameters must be an object JSON Schema")
        json_string(self.parameters)


@dataclass(frozen=True)
class ProviderState:
    """Opaque response blocks. Preserve them unchanged when continuing the same model."""

    provider: str
    model: str
    payload: Any = field(repr=False)
    fingerprint: str


@dataclass(frozen=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: Sequence[ToolCall] = ()
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    provider_state: ProviderState | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not all(isinstance(call, ToolCall) for call in self.tool_calls):
            raise InvalidRequestError("tool_calls must contain ToolCall objects")
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise InvalidRequestError("Unknown message role")
        if not isinstance(self.content, str):
            raise InvalidRequestError(
                "Message.content must be text; multimodal input is not supported"
            )
        if self.tool_calls and self.role != "assistant":
            raise InvalidRequestError("Only assistant messages can contain tool calls")
        if self.role == "tool":
            if not self.tool_call_id or not self.name:
                raise InvalidRequestError("Tool results require tool_call_id and name")
        elif self.tool_call_id is not None or self.name is not None or self.is_error:
            raise InvalidRequestError("Tool result fields are only valid on tool messages")
        if self.provider_state is not None and self.role != "assistant":
            raise InvalidRequestError("Only assistant messages can contain provider state")

    @classmethod
    def tool_result(cls, call: ToolCall, output: Any, *, is_error: bool = False) -> Message:
        return cls(
            role="tool",
            content=output if isinstance(output, str) else json_string(output),
            tool_call_id=call.id,
            name=call.name,
            is_error=is_error,
        )

    def fingerprint(self) -> str:
        data = {"content": self.content, "tool_calls": [asdict(c) for c in self.tool_calls]}
        return hashlib.sha256(json_string(data).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable history, including state needed after process restart."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Message:
        values = deepcopy(data)
        values["tool_calls"] = tuple(ToolCall(**c) for c in values.get("tool_calls", ()))
        if values.get("provider_state") is not None:
            values["provider_state"] = ProviderState(**values["provider_state"])
        return cls(**values)


@dataclass(frozen=True)
class LLMRequest:
    messages: Sequence[Message]
    tools: Sequence[ToolDefinition] = ()
    max_output_tokens: int = 4096
    temperature: float | None = None
    tool_choice: ToolChoice = "auto"
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "tools", tuple(self.tools))
        self.validate()

    def validate(self) -> None:
        if not self.messages or not all(isinstance(m, Message) for m in self.messages):
            raise InvalidRequestError("messages must contain Message objects")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise InvalidRequestError("max_output_tokens must be a positive integer")
        if self.temperature is not None and (
            not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
            or not 0 <= self.temperature <= 2
        ):
            raise InvalidRequestError("temperature must be between 0 and 2, or None")
        if self.tool_choice not in {"auto", "none", "required"}:
            raise InvalidRequestError("tool_choice must be auto, none or required")
        if self.tool_choice == "required" and not self.tools:
            raise InvalidRequestError("tool_choice=required needs tools")
        if not all(isinstance(t, ToolDefinition) for t in self.tools):
            raise InvalidRequestError("tools must contain ToolDefinition objects")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise InvalidRequestError("Tool names must be unique")
        for tool in self.tools:
            tool.__post_init__()
        if not isinstance(self.extra, dict):
            raise InvalidRequestError("extra must be a JSON object")
        try:
            json_string(self.extra)
        except (ValueError, TypeError):
            raise InvalidRequestError("extra must be JSON serializable") from None
        pending: dict[str, str] = {}
        seen_ids: set[str] = set()
        conversation_started = False
        for message in self.messages:
            if message.role == "system":
                if conversation_started:
                    raise InvalidRequestError("System messages must precede the conversation")
                continue
            if not conversation_started and message.role != "user":
                raise InvalidRequestError("Conversation must start with a user message")
            conversation_started = True
            if message.role == "tool":
                if pending.get(message.tool_call_id) != message.name:
                    raise InvalidRequestError("Tool result does not match a pending call")
                if next(iter(pending)) != message.tool_call_id:
                    raise InvalidRequestError(
                        "Return parallel tool results in the original call order"
                    )
                del pending[message.tool_call_id]
            else:
                if pending:
                    raise InvalidRequestError("Return all tool results before the next message")
                for call in message.tool_calls:
                    call.__post_init__()
                    if call.id in seen_ids:
                        raise InvalidRequestError("Tool call IDs must be unique in a conversation")
                    pending[call.id] = call.name
                    seen_ids.add(call.id)
        if not conversation_started or pending:
            raise InvalidRequestError("Request needs a conversation with no pending tool results")


@dataclass(frozen=True)
class Usage:
    # Input includes cache reads/writes; output includes reasoning when reported.
    # Missing counters remain None instead of being misrepresented as zero.
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class LLMResponse:
    provider: str
    model: str
    message: Message
    finish_reason: FinishReason
    usage: Usage = field(default_factory=Usage)
    id: str | None = None
    provider_finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    # A length-limited response contained tool blocks. They are intentionally not
    # decoded into executable ToolCalls; even parseable arguments may be incomplete.
    truncated_tool_calls: bool = False

    @property
    def text(self) -> str:
        return self.message.content

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(self.message.tool_calls)

    def to_message(self) -> Message:
        return deepcopy(self.message)
