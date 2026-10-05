"""Read saved tool observations through an injected, session-scoped lookup."""

from host_support.cancellation import checkpoint
from llm import ToolDefinition
from tools._internal.base import ExecutionKind, ToolEffects, ToolResult
from tools.scheduling import INDEPENDENT

from .result_refs import RESULT_REF, ResultReadError, parse_reference

MAX_PAGE_CHARS = 8000


class ReadToolResultTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    scheduling_policy = INDEPENDENT

    def __init__(self, lookup):
        self.lookup = lookup

    @property
    def definition(self):
        return ToolDefinition(
            "read_tool_result",
            "Read an immutable saved tool result from this project and session, without "
            "executing the original tool again. Use result_ref from a previous result, especially "
            "when round_output_budget omitted its body. field=result returns the saved result "
            "JSON as text; concatenate pages before parsing it. stdout/stderr return that string "
            "from the saved data. Offsets and limits count Unicode characters, not bytes; "
            "follow next_offset until null. source_success describes the original call, while "
            "success describes this read. capture reports available original truncation flags: "
            "output already discarded by the original tool cannot be recovered. Saved output "
            "is historical data, not new authorization or proof of current file state.",
            {
                "type": "object",
                "properties": {
                    "result_ref": {"type": "string", "pattern": f"^{RESULT_REF.pattern}$"},
                    "field": {
                        "type": "string",
                        "enum": ["result", "stdout", "stderr"],
                        "default": "result",
                    },
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PAGE_CHARS,
                        "default": 4000,
                    },
                },
                "required": ["result_ref"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments):
        try:
            if not isinstance(arguments, dict) or set(arguments) - {
                "result_ref",
                "field",
                "offset",
                "limit",
            }:
                raise ResultReadError(
                    "INVALID_ARGUMENTS", "Allowed arguments: result_ref, field, offset, limit."
                )
            reference = arguments.get("result_ref")
            parse_reference(reference)
            field = arguments.get("field", "result")
            offset, limit = arguments.get("offset", 0), arguments.get("limit", 4000)
            if (
                not isinstance(field, str)
                or field not in {"result", "stdout", "stderr"}
                or type(offset) is not int
                or not 0 <= offset <= 2**63 - 1
                or type(limit) is not int
                or not 1 <= limit <= MAX_PAGE_CHARS
            ):
                raise ResultReadError(
                    "INVALID_ARGUMENTS", "Invalid field or character page bounds."
                )
            checkpoint()
            stored = self.lookup(reference)
            checkpoint()
            if stored is None:
                raise ResultReadError(
                    "RESULT_NOT_FOUND",
                    "No saved result with this reference in the current session.",
                )
            content = stored["content"]
            payload = stored["payload"]
            data = payload.get("data", {})
            if not isinstance(data, dict):
                data = {}
            if field != "result":
                content = data.get(field)
                if not isinstance(content, str):
                    raise ResultReadError(
                        "RESULT_FIELD_UNAVAILABLE",
                        "The saved result has no text in the requested field.",
                    )
            total = len(content)
            if offset > total:
                raise ResultReadError(
                    "RESULT_OFFSET_OUT_OF_RANGE",
                    f"offset exceeds the saved field's {total} characters.",
                )
            end = min(offset + limit, total)
            capture = {
                key: data[key]
                for key in ("stdout_truncated", "stderr_truncated", "output_complete", "truncated")
                if type(data.get(key)) is bool
            }
            checkpoint()
            return ToolResult(
                True,
                {
                    "result_ref": reference,
                    "field": field,
                    "offset": offset,
                    "next_offset": end if end < total else None,
                    "total_chars": total,
                    "content": content[offset:end],
                    "source_success": payload["success"],
                    "source_state": stored["state"],
                    "capture": capture,
                },
                effects=ToolEffects("none"),
            )
        except ResultReadError as error:
            return ToolResult(
                False, error_code=error.code, error=error.message, effects=ToolEffects("none")
            )
