"""Host-owned permissions, separate from scheduling and post-execution receipts.

The first review policy admits exact audited implementations, not tool names,
subclasses or self-declared 'read only' flags. Host plugins are trusted code;
this policy does not sandbox arbitrary Python imported into the host process.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessPolicy:
    mode: str = "develop"
    # The composing host may admit specific control implementations. This is
    # never populated from model arguments, project configuration or snapshots.
    trusted_readers: tuple[type, ...] = ()
    permissions: object = None

    def __post_init__(self):
        if self.mode not in {"develop", "review"}:
            raise ValueError("Invalid access mode")
        if type(self.trusted_readers) is not tuple or any(
            not isinstance(reader, type) for reader in self.trusted_readers
        ):
            raise ValueError("trusted_readers must contain concrete implementation types")
        if self.permissions is not None:
            from .permissions import PermissionManager

            if (
                type(self.permissions) is not PermissionManager
                or self.permissions.profile.access_mode != self.mode
            ):
                raise ValueError("Permission manager must match the host access mode")

    @property
    def read_only(self):
        return self.mode == "review"

    def allows(self, tool):
        from .permissions import PermissionTool

        if type(tool) is PermissionTool:
            return self.permissions is not None and tool.manager is self.permissions
        if not self.read_only:
            return True
        from .execute import GetExecutionEnvironmentTool
        from .filesystem import (
            FindFileTool,
            GetPathInfoTool,
            ListFileTool,
            ReadFileTool,
            SearchFilesTool,
        )
        from .git_tools import GitDiffTool, GitLogTool, GitShowTool, GitStatusTool
        from .tool_groups import LoadToolGroupTool

        if type(tool) is GetExecutionEnvironmentTool:
            return tool.execution_allowed is False
        if type(tool) in {GitDiffTool, GitLogTool, GitShowTool, GitStatusTool}:
            from ._internal.review_git import ReviewGitRunner

            return tool.execution_allowed is False and type(tool.runner) is ReviewGitRunner
        return (
            type(tool)
            in {
                FindFileTool,
                GetPathInfoTool,
                ListFileTool,
                ReadFileTool,
                SearchFilesTool,
                LoadToolGroupTool,
            }
            or type(tool) in self.trusted_readers
        )
