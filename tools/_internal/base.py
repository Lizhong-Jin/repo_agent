"""Common tool result, ready to send back through the provider-neutral LLM interface."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
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
class ToolEffects:
    """Internal receipt metadata supplied by an implementation or adapter.

    'reported' describes known changes, not a guarantee of exhaustive coverage.
    'unknown' may carry process diagnostics without claiming known file changes.
    This metadata is serialized by workers, but never included in model messages.
    """

    status: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.status not in {"unknown", "none", "reported"}:
            raise ValueError("Invalid tool effects status")
        if not isinstance(self.details, dict):
            raise TypeError("Tool effects details must be a dictionary")
        object.__setattr__(self, "details", deepcopy(self.details))

    def to_record(self):
        return {"effects": self.status, "result": deepcopy(self.details)}

    @classmethod
    def process(cls, data):
        """Diagnostics do not establish what an arbitrary process changed."""
        return cls(
            details={
                key: data[key]
                for key in (
                    "exit_code",
                    "timed_out",
                    "cleanup_status",
                    "cleanup_error",
                    "cleanup_diagnostics",
                    "inner_cleanup_error",
                )
                if key in data
            }
        )


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None
    effects: ToolEffects = field(default_factory=ToolEffects)

    def __post_init__(self):
        # JSON workers return a nested dictionary; legacy payloads omit it.
        if isinstance(self.effects, dict):
            object.__setattr__(self, "effects", ToolEffects(**self.effects))
        elif not isinstance(self.effects, ToolEffects):
            raise TypeError("effects must be ToolEffects")

    def with_effects(self, status="reported", *, details=None):
        """Explicitly opt result data (or selected details) into the receipt."""
        return replace(self, effects=ToolEffects(status, self.data if details is None else details))

    def with_file_changes(self, changes):
        """Keep existing effects fields while adding internal operation evidence."""
        return self.with_effects(details={**self.data, "file_changes": changes})

    def to_message(self, call: ToolCall) -> Message:
        output = {"success": self.success, "data": self.data}
        if not self.success:
            output["error"] = {"code": self.error_code, "message": self.error}
        return Message.tool_result(call, output, is_error=not self.success)
