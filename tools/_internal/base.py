"""Common tool result, ready to send back through the provider-neutral LLM interface."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from llm import Message, ToolCall, ToolDefinition
from tools.scheduling import SchedulingPolicy, scheduling_policy_of


class ExecutionKind(Enum):
    """Trusted host metadata, never part of the model's argument schema."""

    HOST_CONTROL = "host_control"
    TRUSTED_FILE = "trusted_file"
    TRUSTED_NETWORK = "trusted_network"
    SANDBOXED_PROCESS = "sandboxed_process"


class Tool(Protocol):
    execution_kind: ExecutionKind
    scheduling_policy: SchedulingPolicy

    @property
    def definition(self) -> ToolDefinition: ...

    def execute(self, arguments: dict[str, Any]) -> "ToolResult": ...


def execution_kind_of(tool: Tool) -> ExecutionKind:
    """Require a concrete declaration; subclasses must reconsider their boundary.

    Proxy instances receive their declaration from the trusted tool factory.
    Strings, inherited defaults and model arguments are never accepted.
    """
    kind = getattr(tool, "__dict__", {}).get(
        "execution_kind", vars(type(tool)).get("execution_kind")
    )
    if not isinstance(kind, ExecutionKind):
        raise ValueError(
            f"{type(tool).__name__} must explicitly declare execution_kind as an ExecutionKind"
        )
    return kind


def validate_tools(tools: list[Tool]) -> list[Tool]:
    for tool in tools:
        execution_kind_of(tool)
        scheduling_policy_of(tool)
    return tools


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
