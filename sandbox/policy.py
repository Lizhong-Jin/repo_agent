"""Host-owned, immutable execution policy."""

import re
from dataclasses import dataclass
from pathlib import PurePath

from tools.file_policy import is_protected_name


@dataclass(frozen=True)
class SandboxPolicy:
    image: str = "repo-agent-sandbox:v1"
    memory: str = "512m"
    cpus: str = "1"
    pids: int = 64
    timeout: int = 130
    max_workspace_bytes: int = 256 * 1024 * 1024

    gpus: str | None = None
    tmpfs_size: str = "64m"
    shm_size: str = "64m"
    max_file_bytes: int = 64 * 1024 * 1024
    command_timeout_seconds: int = 120
    python_timeout_seconds: int = 30

    def __post_init__(self):
        if self.gpus is not None and (
            not isinstance(self.gpus, str)
            or not re.fullmatch(r"all|[0-9]+|GPU-[a-fA-F0-9-]+", self.gpus)
        ):
            raise ValueError("gpus must be all, a device index, or an NVIDIA GPU UUID")
        for name in (
            "timeout",
            "max_file_bytes",
            "command_timeout_seconds",
            "python_timeout_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.timeout <= max(self.command_timeout_seconds, self.python_timeout_seconds):
            raise ValueError("Container timeout must exceed tool timeouts")

    @classmethod
    def for_profile(cls, profile: str, *, image: str | None = None, gpus: str | None = None):
        if profile == "standard":
            if gpus is not None:
                raise ValueError("GPU selection requires the cuda profile")
            return cls(image=image or "repo-agent-sandbox:v1")
        if profile == "cuda":
            return cls(
                image=image or "repo-agent-sandbox:v1",
                gpus=gpus or "all",
                memory="8g",
                cpus="4",
                pids=256,
                timeout=930,
                tmpfs_size="2g",
                shm_size="1g",
                max_file_bytes=1024 * 1024 * 1024,
                command_timeout_seconds=900,
                python_timeout_seconds=900,
            )
        raise ValueError(f"Unknown sandbox profile: {profile}")

    def excluded(self, relative: str) -> bool:
        return is_protected_name(relative) or any(
            part.lower()
            in {
                ".venv",
                "venv",
                "node_modules",
                "__pycache__",
                ".pytest_cache",
            }
            for part in PurePath(relative).parts
        )
