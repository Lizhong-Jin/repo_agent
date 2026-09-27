"""Shared execution contract; snapshots and writeback belong to Docker sessions."""

from pathlib import Path
from typing import Protocol

from tools._internal.base import ToolResult


class SandboxBackend(Protocol):
    def execute(self, workspace: Path, name: str, arguments: dict) -> ToolResult: ...
