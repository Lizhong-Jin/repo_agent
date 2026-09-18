"""Single request worker, invoked only inside the image."""

import json
from dataclasses import asdict
from pathlib import Path

from tools.factory import create_default_tools


def main():
    request = json.loads(Path("/request.json").read_text())
    tools = {
        tool.definition.name: tool
        for tool in create_default_tools(
            "/workspace", isolated_execution=True, **request.get("tool_limits", {})
        )
    }
    result = tools[request["name"]].execute(request["arguments"])
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
