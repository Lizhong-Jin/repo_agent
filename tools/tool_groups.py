"""Declarative visibility groups; execution capabilities still come from the host.

Move a tool name between the tuples below to change its group. Registered tools
not mentioned in any group are initially visible, including host-provided tools.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from llm import ToolDefinition

from ._internal.base import ExecutionKind, ToolResult
from .scheduling import SERIAL


@dataclass(frozen=True)
class ToolGroup:
    name: str
    description: str
    tools: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name):
            raise ValueError("Tool group names must be lowercase identifiers")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool groups require a description")
        if isinstance(self.tools, str):
            raise ValueError("Tool group tools must be a sequence of names")
        object.__setattr__(self, "tools", tuple(self.tools))
        if not self.tools or any(not isinstance(name, str) or not name for name in self.tools):
            raise ValueError("Tool groups require non-empty tool names")
        if len(set(self.tools)) != len(self.tools) or "load_tool_group" in self.tools:
            raise ValueError("Duplicate or reserved tool name in group")


DEFAULT_TOOL_GROUPS = (
    ToolGroup(
        "file_editing",
        "Create, edit, patch, move or delete workspace files and directories.",
        ("write_file", "edit_file", "apply_patch", "make_directory", "delete_file", "move_file"),
    ),
    ToolGroup(
        "coding",
        "Code execution and checking, including the following tools: "
        "git tools, inspect Git status and diffs; "
        "execution tools, run commands, tests or Python in the configured sandbox; "
        "code_intelligence tools, find symbols, definitions, references, "
        "hover info and diagnostics;",
        (
            "git_status",
            "git_diff",
            "run_command",
            "run_shell",
            "run_python",
            "get_symbols",
            "go_to_definition",
            "find_references",
            "get_diagnostics",
            "get_hover",
            "search_workspace_symbols",
        ),
    ),
)


class ToolGroupRegistry:
    """Per-runtime visibility state, intersected with already-authorized tools."""

    def __init__(self, groups: Sequence[ToolGroup], available: Iterable[str]):
        self.groups: dict[str, ToolGroup] = {}
        self.membership: dict[str, str] = {}
        self.available = frozenset(available)
        self._loaded: dict[str, None] = {}
        for group in groups:
            if not isinstance(group, ToolGroup):
                raise ValueError("Expected ToolGroup entries")
            if group.name in self.groups:
                raise ValueError(f"Duplicate tool group: {group.name}")
            self.groups[group.name] = group
            for name in group.tools:
                if name in self.membership:
                    raise ValueError(f"Tool belongs to multiple groups: {name}")
                self.membership[name] = group.name

    @property
    def loaded(self) -> tuple[str, ...]:
        return tuple(self._loaded)

    def enabled(self, tool: str) -> bool:
        group = self.membership.get(tool)
        return group is None or group in self._loaded

    def available_members(self, group: str) -> tuple[str, ...]:
        return tuple(name for name in self.groups[group].tools if name in self.available)

    def restore(self, names: Sequence[str]) -> tuple[str, ...]:
        """Revalidate saved names against the current catalog and environment."""
        if isinstance(names, (str, bytes)) or not all(isinstance(name, str) for name in names):
            raise ValueError("Loaded tool groups must be a sequence of names")
        allowed = dict.fromkeys(
            name for name in names if name in self.groups and self.available_members(name)
        )
        self._loaded = allowed
        return tuple(name for name in names if name not in allowed)

    def load(self, name: str) -> ToolResult:
        if name not in self.groups:
            return ToolResult(
                False,
                error_code="UNKNOWN_TOOL_GROUP",
                error=f"Unknown group. Groups: {', '.join(self.groups)}.",
            )
        members = self.available_members(name)
        if not members:
            return ToolResult(
                False,
                error_code="TOOL_GROUP_UNAVAILABLE",
                error="This environment provides none of this group's tools. "
                "Loading a group cannot enable execution permissions.",
            )
        already_loaded = name in self._loaded
        self._loaded[name] = None
        return ToolResult(
            True,
            {
                "group": name,
                "tools": list(members),
                "already_loaded": already_loaded,
                "unavailable_tools": [
                    tool for tool in self.groups[name].tools if tool not in members
                ],
                "message": "Tool definitions will be available in the next model request. "
                "No grouped tool has been executed.",
            },
        )


class LoadToolGroupTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    scheduling_policy = SERIAL

    def __init__(self, registry: ToolGroupRegistry):
        self.registry = registry

    @property
    def definition(self) -> ToolDefinition:
        catalog = "\n".join(
            f"{name}: {group.description} Available tools: "
            + (", ".join(self.registry.available_members(name)) or "none in this environment")
            for name, group in self.registry.groups.items()
        )
        return ToolDefinition(
            "load_tool_group",
            "Load specialized tool definitions for the current task. Load only relevant groups; "
            "wait for the next response before calling newly enabled tools. Loading is idempotent "
            "and does not grant new permissions. Groups remain loaded until the conversation is "
            "cleared or a new session starts.\n" + catalog,
            {
                "type": "object",
                "properties": {
                    "group": {"type": "string", "enum": list(self.registry.groups)},
                },
                "required": ["group"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments) -> ToolResult:
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"group"}
            or not isinstance(arguments["group"], str)
            or not arguments["group"]
        ):
            return ToolResult(
                False,
                error_code="INVALID_ARGUMENTS",
                error="Provide exactly one non-empty string argument: group.",
            )
        return self.registry.load(arguments["group"])
