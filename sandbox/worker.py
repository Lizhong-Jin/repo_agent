"""Single request worker, invoked inside Docker or a native sandbox."""

import json
from dataclasses import asdict, replace
from pathlib import Path

from tools._internal.lsp_config import LspRegistry, default_lsp_registry
from tools.dispatch import ToolDispatcher
from tools.factory import create_default_tools


def execute_request(request, workspace):
    # This entry point is launched only after native/Docker establishes isolation.
    dispatcher = ToolDispatcher(inside_sandbox=True)
    registry = None
    project = request.get('execution_context', {}).get('python_environments', {}).get('project')
    if project:
        registry = LspRegistry(tuple(
            replace(config, settings={'pylsp': {'plugins': {'jedi': {'environment': project}}}})
            if config.language_id == 'python' else config
            for config in default_lsp_registry().languages
        ))
    for tool in create_default_tools(
        workspace, isolated_execution=True,
        **({"lsp_registry": registry} if registry else {}),
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
