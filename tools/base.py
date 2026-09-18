"""Common tool result, ready to send back through the provider-neutral LLM interface."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from llm import Message, ToolCall, ToolDefinition


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def execute(self, arguments: dict[str, Any]) -> "ToolResult": ...


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None

    def to_message(self, call: ToolCall) -> Message:
        output = {"success": self.success, "data": self.data}
        if not self.success:
            output["error"] = {"code": self.error_code, "message": self.error}
        return Message.tool_result(call, output, is_error=not self.success)
