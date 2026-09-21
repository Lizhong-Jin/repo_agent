"""Single request worker, invoked inside Docker or a native sandbox."""

import json
from dataclasses import asdict
from pathlib import Path

from tools.factory import create_default_tools


def execute_request(request, workspace):
    tools = {
        tool.definition.name: tool
        for tool in create_default_tools(
            workspace, isolated_execution=True,
            execution_context=request.get("execution_context"),
            **request.get("tool_limits", {})
        )
    }
    result = tools[request["name"]].execute(request["arguments"])
    print(json.dumps(asdict(result)))


def main():
    execute_request(json.loads(Path("/request.json").read_text()), "/workspace")


if __name__ == "__main__":
    main()
