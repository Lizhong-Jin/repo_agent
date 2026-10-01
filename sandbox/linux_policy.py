"""Linux mount policy planning. Filesystem observations live for ONE scan only."""

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

from host_support.cancellation import checkpoint

from .linux_mounts import MountTable
from .policy_scan import PolicyPlan as PolicyPlan
from .policy_scan import ScanFailure, ScanRequest, ScanResult
from .policy_scan import outermost as outermost


@contextmanager
def open_directory(path):
    """Validate the final component while opening; keep fd alive for DT_UNKNOWN."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError(f"Linux native 扫描期间目录变为符号链接或非目录：{path}") from error
        raise
    try:
        yield fd
    finally:
        os.close(fd)


class PolicyScan:
    """Cache sparse directory facts, then apply each exposure path's own rules.

    Directory aliases reuse enumeration AND filename classification. Ordinary
    files are discarded immediately; memory is proportional to directories and
    potentially protected entries, not every file in a large interpreter tree.
    Workspace views are never reused. Bind mounts with different canonical paths
    are intentionally not deduplicated merely because their inode IDs match.
    """

    def __init__(
        self,
        plan,
        *,
        git_read,
        check_workspace_file=None,
        mount_table=None,
        pruned_paths=(),
        extra_roots=(),
    ):
        self.plan, self.git_read = plan, git_read
        self.check_workspace_file = check_workspace_file
        self.workspace = str(plan.workspace)
        self.workspace_prefix = self.workspace + os.sep
        self.cache = {}
        self.mount_table = mount_table if mount_table is not None else MountTable.read()
        self.pruned_paths = frozenset(map(str, pruned_paths))
        self.extra_roots = tuple(extra_roots)
        self.active_root = None
        self.active_bucket = None
        self.buckets = {}
        self.metrics = dict(
            scan_ms=0.0,
            enumeration_ms=0.0,
            rules_ms=0.0,
            mapping_ms=0.0,
            metadata_ms=0.0,
            directory_opens=0,
            metadata_checks=0,
            by_root_mount=[],
            directories_scanned=0,
            directories_reused=0,
            entries_classified=0,
            roots=0,
            alias_roots=0,
            masks=0,
            git_paths=0,
            complete=False,
            workspace_directories_scanned=0,
            workspace_entries_checked=0,
            workspace_validation_ms=0.0,
        )

    def _validates_workspace(self, directory):
        return self.check_workspace_file is not None and (
            directory == self.workspace or directory.startswith(self.workspace_prefix)
        )

    def _record(self, key, value):
        self.metrics[key] += value
        if self.active_bucket is not None:
            self.active_bucket[key] = self.active_bucket.get(key, 0) + value

    def _entries(self, directory, canonical, reuse):
        checkpoint()
        if reuse and canonical in self.cache:
            started = perf_counter()
            if directory != self.active_root:
                info = os.lstat(directory)
                self._record("metadata_checks", 1)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"Linux native 扫描期间目录变为符号链接或非目录：{directory}")
            self._record("metadata_ms", (perf_counter() - started) * 1000)
            self._record("directories_reused", 1)
            return self.cache[canonical]
        self._record("directories_scanned", 1)
        started = perf_counter()
        try:
            # Root aliases were resolved/verified separately. Descendants must
            # never be followed if swapped for a symlink after classification.
            with open_directory(canonical if directory == self.active_root else directory) as fd:
                self._record("directory_opens", 1)
                self._record("metadata_ms", (perf_counter() - started) * 1000)
                started = perf_counter()
                with os.scandir(fd) as iterator:
                    entries = list(iterator)
                self._record("enumeration_ms", (perf_counter() - started) * 1000)
                facts = self._classify(directory, entries)
        except PermissionError as error:
            if error.errno not in {errno.EACCES, errno.EPERM}:
                raise
            if self._validates_workspace(directory):
                raise
            facts = error.errno
        if reuse:
            self.cache[canonical] = facts
        return facts

    def _classify(self, directory, entries):
        if self._validates_workspace(directory):
            started = perf_counter()
            self._record("workspace_directories_scanned", 1)
            try:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        path = os.path.join(directory, entry.name)
                        self.check_workspace_file(path, os.lstat(path))
                        self._record("workspace_entries_checked", 1)
            finally:
                self._record("workspace_validation_ms", (perf_counter() - started) * 1000)
        started = perf_counter()
        facts = []
        for entry in entries:
            name = entry.name
            protected = self.plan.name_rules.matches_leaf(name)
            is_directory = entry.is_dir(follow_symlinks=False)
            if is_directory or protected or name in self.plan.protected_names:
                facts.append((name, is_directory, entry.is_symlink(), protected))
        self._record("entries_classified", len(entries))
        self._record("rules_ms", (perf_counter() - started) * 1000)
        return tuple(facts)

    def run(self, read_paths):
        started = perf_counter()
        try:
            return self._run(read_paths)
        finally:
            self.metrics["scan_ms"] = (perf_counter() - started) * 1000
            self.metrics["mapping_ms"] = max(
                0.0,
                self.metrics["scan_ms"]
                - self.metrics["enumeration_ms"]
                - self.metrics["rules_ms"]
                - self.metrics["workspace_validation_ms"]
                - self.metrics["metadata_ms"],
            )
            self.metrics["by_root_mount"] = list(self.buckets.values())
            self.cache.clear()  # Never retain observations across tool calls.

    def _run(self, read_paths):
        checkpoint()
        masks, git_paths, identities = [], [], []
        workspace = self.plan.workspace
        for root in (*self.plan.roots(read_paths), *self.extra_roots):
            self.active_root = str(root)
            self.metrics["roots"] += 1
            fixed = any(root == p or root.is_relative_to(p) for p in self.plan.protected_paths)
            git = root.name.lower() == ".git" and self.git_read and not fixed
            root_masked = fixed or (self.plan.name_rules.matches_leaf(root.name) and not git)
            if root_masked:
                masks.append(root)
                if not self._validates_workspace(str(root)):
                    continue
            if git:
                git_paths.append(root)
            if self.active_root in self.pruned_paths:
                continue
            if not root.is_dir():
                continue
            canonical_root = root.resolve(strict=True)
            info = root.stat()
            identities.append((root, canonical_root, info.st_dev, info.st_ino))
            self.metrics["alias_roots"] += root != canonical_root
            reuse = not (
                root.is_relative_to(workspace)
                or canonical_root.is_relative_to(workspace)
                or workspace.is_relative_to(canonical_root)
            )
            mount = self.mount_table.containing(canonical_root)
            pending = [(str(root), str(canonical_root), root_masked, mount)]
            while pending:
                directory, canonical, masked, mount = pending.pop()
                if directory in self.pruned_paths:
                    continue
                mount = self.mount_table.mounts.get(canonical, mount)
                key = (str(root), mount.path if mount else "")
                self.active_bucket = self.buckets.setdefault(
                    key,
                    {
                        "root": str(root),
                        "mount": mount.path if mount else None,
                        "filesystem": mount.filesystem if mount else None,
                    },
                )
                facts = self._entries(directory, canonical, reuse)
                if isinstance(facts, int):
                    path = Path(directory)
                    if path.is_relative_to(workspace) or path.resolve(strict=True).is_relative_to(
                        workspace
                    ):
                        raise PermissionError(facts, "Cannot scan workspace", directory)
                    # Mask an unreadable system subtree in EVERY exposure path.
                    masks.append(path)
                    continue
                fixed_names = self.plan.protected_children.get(directory, ())
                for name, is_directory, is_link, protected in facts:
                    child_masked = masked
                    fixed = name in fixed_names
                    git = name.lower() == ".git" and self.git_read and not fixed
                    if not masked and (fixed or (protected and not git)):
                        path = Path(directory) / name
                        masks.append(
                            path.parent if is_link and not path.is_relative_to(workspace) else path
                        )
                        child_masked = True
                    elif not masked and git:
                        git_paths.append(Path(directory) / name)
                    # Masked workspace trees still need hard-link/special-file
                    # validation; suppress only their redundant policy entries.
                    if is_directory and (not child_masked or self._validates_workspace(directory)):
                        pending.append(
                            (
                                os.path.join(directory, name),
                                os.path.join(canonical, name),
                                child_masked,
                                mount,
                            )
                        )
        # Resolve roots afresh: a retargeted alias must never reuse another tree's
        # observations. This is not a filesystem snapshot or a TOCTOU guarantee.
        for root, canonical, device, inode in identities:
            info = root.stat()
            if root.resolve(strict=True) != canonical or (info.st_dev, info.st_ino) != (
                device,
                inode,
            ):
                raise ValueError(f"Linux native 扫描期间挂载目录发生变化：{root}")
        self.mount_table.verify()
        masks = outermost(masks)
        for path in (*masks, *git_paths):
            if path.is_symlink():
                raise ValueError(f"Linux native 受保护挂载点不能是符号链接：{path}")
        checkpoint()
        self.metrics.update(masks=len(masks), git_paths=len(git_paths), complete=True)
        return masks, git_paths


def validate_workspace_file(path: str, info: os.stat_result):
    """Linux validation shared by the reference preflight and Python scanner."""
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ValueError(f"Native 工作区含硬链接，拒绝执行：{path}")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise ValueError("Linux native 工作区含 socket/FIFO/设备等特殊文件，拒绝执行")


class PythonPolicyScanner:
    """Stateless implementation of PolicyScanner; each call owns its observations.

    PolicyScan stays available as a Python implementation detail for diagnostic
    probes. The public batch contract never accepts its per-file callback.
    """

    def scan(self, plan: PolicyPlan, request: ScanRequest) -> ScanResult:
        scan = PolicyScan(
            plan,
            git_read=request.git_read,
            mount_table=MountTable(request.mount_snapshot),
            pruned_paths=request.pruned_paths,
            extra_roots=request.extra_roots,
            check_workspace_file=validate_workspace_file,
        )
        try:
            masks, git_paths = scan.run(request.read_paths)
        except BaseException as error:
            raise ScanFailure(error, scan.metrics) from error
        return ScanResult(tuple(masks), tuple(git_paths), scan.metrics)
