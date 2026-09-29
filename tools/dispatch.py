"""Global dispatch of host-owned tools; declarations do not grant OS privileges.

Only trusted application code may register implementations. This dispatcher is
not an isolation boundary for third-party Python plugins already in the host.
"""

from host_support.cancellation import checkpoint, current_cancellation, defer_cancellation

from ._internal.base import ExecutionKind, ToolResult, execution_kind_of


class ToolDispatcher:
    def __init__(self, *, inside_sandbox=False):
        if type(inside_sandbox) is not bool:
            raise ValueError("inside_sandbox must be a boolean")
        # Only the worker, after OS isolation has been established, enables this.
        self.inside_sandbox = inside_sandbox
        self.tools = {}
        self._kinds = {}

    def register(self, tool):
        kind = execution_kind_of(tool)
        name = tool.definition.name
        if name in self.tools:
            raise ValueError(f"Duplicate tool name: {name}")
        if self.inside_sandbox and kind not in {
            ExecutionKind.TRUSTED_FILE,
            ExecutionKind.SANDBOXED_PROCESS,
        }:
            raise ValueError(f"Host tool {name} cannot be registered in a sandbox worker")
        self.tools[name] = tool
        self._kinds[name] = kind

    def execute(self, name, arguments):
        checkpoint()
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(False, error_code="UNKNOWN_TOOL", error=f"Unknown tool: {name}")
        kind = execution_kind_of(tool)
        if kind is not self._kinds.get(name):
            raise ValueError(f"Tool {name} execution_kind changed after registration")
        handlers = {
            ExecutionKind.HOST_CONTROL: self._host,
            ExecutionKind.TRUSTED_FILE: self._file,
            ExecutionKind.TRUSTED_NETWORK: self._network,
            ExecutionKind.SANDBOXED_PROCESS: self._process,
        }
        context = current_cancellation()
        record = {"name": name, "status": "started", "effects": "unknown"}
        if context is not None:
            context.tools.append(record)
        result = handlers[kind](tool, arguments)
        record["status"] = "completed" if result.success else "failed"
        # Preserve tool-reported effects only; do not infer subprocess changes.
        record["result"] = {
            key: value
            for key, value in result.data.items()
            if key
            in {
                "path",
                "source",
                "destination",
                "created",
                "deleted",
                "moved",
                "committed_files",
                "committed",
                "not_committed",
                "files_changed",
                "changes",
                "bytes_written",
                "sha256_before",
                "sha256_after",
                "cleanup_status",
                "cleanup_error",
            }
        }
        if name in {
            "write_file",
            "edit_file",
            "apply_patch",
            "make_directory",
            "delete_file",
            "move_file",
        }:
            record["effects"] = "reported"
        checkpoint()
        return result

    @staticmethod
    def _host(tool, arguments):
        return tool.execute(arguments)

    @staticmethod
    def _file(tool, arguments):
        # Native proxies use the descriptor-based file service; Docker proxies
        # retain their private workspace and writeback guard. Local keeps its API.
        with defer_cancellation():
            return tool.execute(arguments)

    @staticmethod
    def _network(tool, arguments):
        # The network tool's host-owned backend enforces endpoint/DNS/body limits.
        return tool.execute(arguments)

    def _process(self, tool, arguments):
        if self.inside_sandbox:
            return tool.execute(arguments)

        # Recognize the actual adapter types, never a model/plugin-supplied flag.
        from sandbox.native import NativeTool
        from sandbox.session import SandboxedTool

        if type(tool) in {NativeTool, SandboxedTool}:
            return tool.execute(arguments)

        # Existing local capabilities: passive environment inspection and Git
        # with repository-provided clean/process filters refused. No subclass
        # or execution_allowed=True instance can use this compatibility route.
        from .execute import GetExecutionEnvironmentTool
        from .git_tools import GitDiffTool, GitStatusTool

        if (
            type(tool) in {GetExecutionEnvironmentTool, GitDiffTool, GitStatusTool}
            and tool.execution_allowed is False
        ):
            return tool.execute(arguments)
        return ToolResult(
            False,
            error_code="SANDBOX_REQUIRED",
            error="Process tools require a native/Docker execution adapter.",
        )
