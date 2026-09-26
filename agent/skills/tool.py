"""Loading returns a frozen skill; executable resources use existing sandbox tools."""

from llm import ToolDefinition
from tools._internal.base import ExecutionKind, ToolResult

from .registry import SkillError, SkillRegistry


class LoadSkillTool:
    execution_kind = ExecutionKind.HOST_CONTROL

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    @property
    def definition(self):
        return ToolDefinition(
            name="load_skill",
            description="Load a skill's instructions by its exact name from the available catalog.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "minLength": 1}},
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments):
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"name"}
            or not isinstance(arguments["name"], str)
            or not arguments["name"]
        ):
            return ToolResult(False, error_code="INVALID_ARGUMENTS", error="需要唯一参数 name")
        try:
            skill = self.registry.get(arguments["name"])
        except SkillError as error:
            return ToolResult(False, error_code="UNKNOWN_SKILL", error=str(error))
        return ToolResult(True, skill.payload())
