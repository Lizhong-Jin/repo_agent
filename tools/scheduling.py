"""Trusted tool declarations, independent of model schemas and execution permissions."""

from dataclasses import dataclass
from enum import Enum


class WorkspaceAccess(Enum):
    NONE = "none"
    READ = "read"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True)
class SchedulingPolicy:
    workspace_access: WorkspaceAccess = WorkspaceAccess.EXCLUSIVE
    session_barrier: bool = True
    reentrant: bool = False

    def __post_init__(self):
        if not isinstance(self.workspace_access, WorkspaceAccess):
            raise ValueError("workspace_access must be a WorkspaceAccess")
        if type(self.session_barrier) is not bool or type(self.reentrant) is not bool:
            raise ValueError("Scheduling flags must be booleans")

    @property
    def parallel(self):
        return (
            self.reentrant
            and not self.session_barrier
            and self.workspace_access is not WorkspaceAccess.EXCLUSIVE
        )


SERIAL = SchedulingPolicy()
READ_ONLY = SchedulingPolicy(WorkspaceAccess.READ, session_barrier=False, reentrant=True)
INDEPENDENT = SchedulingPolicy(WorkspaceAccess.NONE, session_barrier=False, reentrant=True)


def scheduling_policy_of(tool):
    # Subclasses must reconsider concurrency explicitly. Old/custom tools remain
    # usable, but inherited or absent declarations never grant parallel execution.
    policy = getattr(tool, "__dict__", {}).get(
        "scheduling_policy", vars(type(tool)).get("scheduling_policy", SERIAL)
    )
    if not isinstance(policy, SchedulingPolicy):
        raise ValueError(f"{type(tool).__name__} has an invalid scheduling_policy")
    return policy
