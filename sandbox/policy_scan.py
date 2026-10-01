"""Data contracts for Linux policy scanners; no filesystem observations or backend imports.

Only PolicyPlan may be reused between calls. Requests/results describe one scan.
The Python implementation is selected by the composition layer, not this module.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from host_support.path_rules import NameRules


def outermost(paths):
    """Prune lexical descendants, retaining aliases and deterministic mount order."""
    result, selected = [], set()
    for path in sorted(set(paths), key=lambda p: (len(p.parts), str(p))):
        if not any(parent in selected for parent in path.parents):
            result.append(path)
            selected.add(path)
    return result


@dataclass(frozen=True)
class PolicyPlan:
    """Pure configuration only: never cache existence, link targets or permissions."""

    workspace: Path
    name_rules: NameRules
    read_paths: tuple
    protected_paths: tuple
    mount_roots: tuple
    scan_roots: tuple
    protected_children: dict
    protected_names: frozenset
    guard_candidates: tuple

    @classmethod
    def compile(cls, workspace, read_paths, protected_paths, *, name_rules: NameRules):
        children, guards = {}, set()
        for path in protected_paths:
            children.setdefault(str(path.parent), set()).add(path.name)
        for path in (*protected_paths, *read_paths):
            for parent in path.parents:
                if parent == workspace or not parent.is_relative_to(workspace):
                    break
                guards.add(parent)
        return cls(
            workspace,
            name_rules,
            read_paths,
            protected_paths,
            tuple(outermost(read_paths)),
            tuple(outermost((workspace, *read_paths))),
            {parent: frozenset(names) for parent, names in children.items()},
            frozenset(path.name for path in protected_paths),
            tuple(sorted(guards, key=lambda p: (len(p.parts), str(p)))),
        )

    def roots(self, read_paths):
        if tuple(read_paths) == self.read_paths:
            return self.scan_roots
        return outermost((self.workspace, *read_paths))


@dataclass(frozen=True)
class ScanRequest:
    """Trusted per-launch configuration, including the captured mount layout."""

    read_paths: tuple[Path, ...]
    git_read: bool
    mount_snapshot: str
    pruned_paths: tuple[Path, ...] = ()
    extra_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    """Only a complete, validated scan produces a result."""

    masks: tuple[Path, ...]
    git_paths: tuple[Path, ...]
    metrics: dict[str, Any]

    def __post_init__(self):
        if self.metrics.get("complete") is not True:
            raise ValueError("An incomplete policy scan cannot produce a ScanResult")


class ScanFailure(BaseException):
    """Carry diagnostics without turning a failed/ cancelled scan into a result.

    The adapter re-raises the original error, including cancellation control flow.
    A future native binding must map its failures to the same Python error kinds.
    """

    def __init__(self, error: BaseException, metrics: dict[str, Any]):
        super().__init__(str(error))
        self.error = error
        self.metrics = metrics


class PolicyScanner(Protocol):
    """Coarse public boundary: no caller-supplied per-entry callbacks."""

    def scan(self, plan: PolicyPlan, request: ScanRequest) -> ScanResult: ...
