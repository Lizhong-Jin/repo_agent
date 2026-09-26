"""Single request worker, invoked inside Docker or a native sandbox."""

import json
from dataclasses import asdict
from pathlib import Path

from tools.dispatch import ToolDispatcher
from tools.factory import create_default_tools


def execute_request(request, workspace):
    # This entry point is launched only after native/Docker establishes isolation.
    dispatcher = ToolDispatcher(inside_sandbox=True)
    for tool in create_default_tools(
        workspace, isolated_execution=True,
        execution_context=request.get("execution_context"),
        **request.get("tool_limits", {})
    ):
        dispatcher.register(tool)
    result = dispatcher.execute(request["name"], request["arguments"])
    print(json.dumps(asdict(result)))


def main():
    execute_request(json.loads(Path("/request.json").read_text()), "/workspace")


if __name__ == "__main__":
    main()
