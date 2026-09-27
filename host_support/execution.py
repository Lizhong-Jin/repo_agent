"""Declared backend capabilities for selection/UI, never an authorization token."""

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCapabilities:
    label: str
    supported: bool
    project_python: bool = False
    gpu: bool = False
    writeback: bool = False


def backend_capabilities(mode, *, platform=None):
    platform = platform or sys.platform
    if mode == "local":
        return BackendCapabilities("local", True)
    if mode == "docker":
        return BackendCapabilities("Docker", True, gpu=True, writeback=True)
    if mode == "native":
        if platform == "darwin":
            return BackendCapabilities("macOS", True, project_python=True)
        if platform == "linux":
            return BackendCapabilities("Linux", True, project_python=True, gpu=True)
        return BackendCapabilities(platform, False)
    raise ValueError(f"Unknown execution mode: {mode}")
