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
    gpu_profiles: tuple[str, ...] = ()


def backend_capabilities(mode, *, platform=None):
    platform = platform or sys.platform
    if mode == "local":
        return BackendCapabilities("local", True)
    if mode == "docker":
        return BackendCapabilities("Docker", True, gpu=True, writeback=True, gpu_profiles=("cuda",))
    if mode == "native":
        if platform == "win32":
            return BackendCapabilities("Windows", True, project_python=True)
        if platform == "darwin":
            return BackendCapabilities(
                "macOS", True, project_python=True, gpu=True, gpu_profiles=("metal",)
            )
        if platform == "linux":
            return BackendCapabilities(
                "Linux", True, project_python=True, gpu=True, gpu_profiles=("cuda",)
            )
        return BackendCapabilities(platform, False)
    raise ValueError(f"Unknown execution mode: {mode}")
