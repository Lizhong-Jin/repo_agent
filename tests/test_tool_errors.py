import json

import pytest

from llm import ToolCall
from tools import ReadFileTool, ToolErrorCode, WriteFileTool, tool_error
from tools.execute import RunCommandTool


@pytest.mark.parametrize("tool_class", [ReadFileTool, WriteFileTool, RunCommandTool])
def test_shared_errors_keep_plain_string_wire_codes(tmp_path, tool_class):
    result = tool_class(tmp_path).execute(None)
    assert type(result.error_code) is str
    assert result.error_code == "INVALID_ARGUMENTS"
    assert result.error == tool_error(ToolErrorCode.INVALID_ARGUMENTS).error
    message = result.to_message(ToolCall("call", "test", {}))
    assert message.is_error
    assert json.loads(message.content)["error"]["code"] == "INVALID_ARGUMENTS"


def test_tool_specific_errors_and_contextual_explanations_remain_supported():
    special = tool_error("NO_MATCH", "old_text does not occur in the file.")
    assert special.error_code == "NO_MATCH"
    assert special.error == "old_text does not occur in the file."
    specific = tool_error(ToolErrorCode.INVALID_ARGUMENTS, "start_line must be positive.")
    assert specific.error == "start_line must be positive."
    with pytest.raises(ValueError, match="explicit message"):
        tool_error("NO_MATCH")
