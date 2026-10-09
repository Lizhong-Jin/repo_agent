"""Trusted native execution contracts, independent of POSIX launch mechanics.

Adapters are selected by backend implementations, never tool arguments. They
must establish OS isolation before executing any project code. There is no
default/unisolated adapter.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from host_support.processes import ProcessResult

from .project_python import ProjectPython

if TYPE_CHECKING:
    from .native_common import NativeBackendBase


@dataclass(frozen=True)
class NativeCall:
    command: tuple[str, ...] | None
    request: dict | None
    cwd: Path
    git_read: bool
    project: bool
    max_output_bytes: int


class NativeCleanupError(RuntimeError):
    """An adapter cannot confirm release of its execution/authorization resources."""


class NativeProcess(Protocol):
    @property
    def last_cleanup_status(self) -> str: ...

    def run(self, *, timeout_seconds: int) -> ProcessResult:
        """Run once, honoring current cancellation; clean up before returning/raising.

        A failed or cancelled run must leave last_cleanup_status truthful. This
        object belongs to one call, not a shared backend or process-global slot.
        """
        ...


class NativeExecutionAdapter(Protocol):
    process_cleanup: str

    def select_project_python(
        self, backend: "NativeBackendBase", explicit: str | Path | None
    ) -> ProjectPython:
        """Discover and authorize an interpreter without executing project code."""
        ...

    def prepare(
        self, backend: "NativeBackendBase", call: NativeCall
    ) -> AbstractContextManager[NativeProcess]:
        """Acquire call resources, construct policy/environment and yield execution.

        Release resources on success, failure and cancellation. Do not suppress
        execution exceptions. If preparation fails, unwind partial acquisition;
        raise NativeCleanupError if resource release cannot be confirmed, even
        before yielding. After yielding, any release failure degrades the backend.
        Adapters must not change scheduling, write execution receipts, or fall
        back to a less restricted executor. Long-lived resources, if needed by a
        future backend, remain owned by that backend's close() implementation.
        """
        ...
