"""Private workspace snapshots, conflict detection, and explicit writeback."""

import difflib
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from host_support.filesystem import (
    mkdir_at,
    open_directory,
    open_file,
    rename_at,
    set_file_mode,
    stat_at,
    unlink_at,
    walk_descriptors,
)
from host_support.paths import find_windows_executable
from tools._internal.base import ExecutionKind, ToolResult, execution_kind_of
from tools._internal.file_policy import runtime_protected_paths
from tools.execute import PROCESS_EXECUTION_TOOLS
from tools.factory import create_default_tools
from tools.scheduling import SERIAL, SchedulingPolicy, scheduling_policy_of

from .concurrency import backend_gate
from .docker import DockerBackend, SandboxBackend
from .policy import SandboxPolicy
from .writeback import Backup, WritebackGuard, atomic_json


def fingerprint(data: bytes, mode: int) -> str:
    if os.name == "nt":
        mode &= ~0o111
    return hashlib.sha256(data).hexdigest() + (":x" if mode & 0o111 else ":-")


def files(
    root: Path, policy: SandboxPolicy, *, strict: bool = False, protected: set[str] | None = None
) -> dict[str, tuple[bytes, int]]:
    """Never follow links; refuse special files and hard links on export."""
    result = {}
    total = 0
    for directory, dirs, names, directory_fd in walk_descriptors(root):
        for name in list(dirs) + names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = stat_at(name, dir_fd=directory_fd)
            excluded = policy.excluded(relative) or any(
                relative == item.rstrip("/") or (item.endswith("/") and relative.startswith(item))
                for item in (protected or set())
            )
            unsafe = stat.S_ISLNK(info.st_mode) or (
                not stat.S_ISDIR(info.st_mode)
                and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            )
            if excluded or unsafe:
                if name in dirs:
                    dirs.remove(name)
                if strict and unsafe:
                    raise ValueError(f"拒绝回写受保护或非普通文件：{relative}")
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            descriptor = open_file(name, dir_fd=directory_fd)
            with os.fdopen(descriptor, "rb") as source:
                current = os.fstat(source.fileno())
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise ValueError(f"文件类型改变：{relative}")
                data = source.read(policy.max_workspace_bytes - total + 1)
            total += len(data)
            if total > policy.max_workspace_bytes:
                raise ValueError("工作区超过 sandbox 大小限制。")
            result[relative] = (data, stat.S_IMODE(current.st_mode))
    if os.name == "nt":
        from host_support.windows_files import validate_snapshot_names

        validate_snapshot_names(result)
    return result


class SandboxedTool:
    scheduling_policy = SERIAL

    def __init__(
        self, definition, session, *, execution_kind, writeback_mode=None, scheduling_policy=SERIAL
    ):
        self.execution_kind = execution_kind
        self.scheduling_policy = SchedulingPolicy(scheduling_policy.workspace_access)
        if execution_kind_of(self) not in {
            ExecutionKind.TRUSTED_FILE,
            ExecutionKind.SANDBOXED_PROCESS,
        }:
            raise ValueError("Docker proxies only accept file/process tools")
        if definition.name in PROCESS_EXECUTION_TOOLS:
            parameters = deepcopy(definition.parameters)
            parameters["properties"]["check_id"] = {
                "type": "string",
                "pattern": "^[a-zA-Z0-9_-]{1,64}$",
                "description": (
                    "Stable ID for one validation. Reuse on corrected retries of the same check; "
                    "never reuse for unrelated checks."
                ),
            }
            definition = replace(
                definition,
                parameters=parameters,
                description=definition.description
                + " For validation, supply a stable check_id from the first attempt. "
                "A successful retry with the same tool, cwd and check_id resolves its earlier "
                "failure even if code changes. "
                "Retain the original assertions; unrelated checks must use different IDs.",
            )
        self.definition = definition
        self.session = session
        self.writeback_mode = writeback_mode

    def execute(self, arguments: dict) -> ToolResult:
        with backend_gate(self.session).hold():
            return self._execute(arguments)

    def _execute(self, arguments: dict) -> ToolResult:
        try:
            execution_arguments = dict(arguments)
            if self.definition.name in PROCESS_EXECUTION_TOOLS and "check_id" in arguments:
                check_id = execution_arguments.pop("check_id")
                if not isinstance(check_id, str) or not re.fullmatch(
                    r"[a-zA-Z0-9_-]{1,64}", check_id
                ):
                    result = ToolResult(
                        False,
                        error_code="INVALID_ARGUMENTS",
                        error="check_id must use 1-64 letters, digits, '_' or '-'.",
                    )
                    self.session.guard.record(self.definition.name, arguments, result)
                    return result
            result = self.session.backend.execute(
                self.session.workspace, self.definition.name, execution_arguments
            )
            if (
                self.definition.name == "get_execution_environment"
                and result.success
                and "execution" in result.data
                and self.writeback_mode is not None
            ):
                data = deepcopy(result.data)
                data["execution"]["writeback_mode"] = self.writeback_mode
                result = replace(result, data=data)
            self.session.guard.record(self.definition.name, arguments, result)
            return result
        except BaseException:
            self.session.guard.needs_review = True
            raise


class SandboxSession:
    def __init__(
        self,
        root: str | Path,
        policy: SandboxPolicy | None = None,
        backend: SandboxBackend | None = None,
        verify_command: list[str] | None = None,
    ):
        self.verify_command = list(verify_command) if verify_command else None
        self.guard = WritebackGuard()
        self.last_backup = None
        self.root = Path(root).resolve(strict=True)
        self.policy = policy or SandboxPolicy()
        self.backend = backend or DockerBackend(self.policy)
        self.directory = Path(tempfile.mkdtemp(prefix="repo-agent-sandbox-")).resolve()
        if self.directory.is_relative_to(self.root):
            self.directory.rmdir()
            raise ValueError("Sandbox 临时目录不能位于项目内部，请调整 TMPDIR。")
        self.workspace = self.directory / "workspace"
        self.workspace.mkdir(mode=0o777)
        self.workspace.chmod(0o777)
        self.protected = set()
        for target in runtime_protected_paths(self.root):
            if target.is_relative_to(self.root):
                relative = target.relative_to(self.root).as_posix()
                self.protected.add(relative + "/" if target.is_dir() else relative)
        source = files(self.root, self.policy, protected=self.protected)
        self.baseline = {name: fingerprint(*entry) for name, entry in source.items()}
        for name, (data, mode) in source.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o777 if mode & 0o111 else 0o666)
        for directory, _, _ in os.walk(self.workspace):
            Path(directory).chmod(0o777)
        self._save()
        # A fresh repository records only the sanitized task baseline, never host history.
        env = {
            "PATH": os.defpath,
            "HOME": str(self.directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Sandbox",
            "GIT_AUTHOR_EMAIL": "sandbox@localhost",
            "GIT_COMMITTER_NAME": "Sandbox",
            "GIT_COMMITTER_EMAIL": "sandbox@localhost",
        }
        git = "git"
        if os.name == "nt":
            git = find_windows_executable("git", exclude=(self.root, self.directory))
            if not git:
                raise ValueError("Git 不可用；请安装 Git for Windows 并加入 PATH。")
            system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            env["PATH"] = os.pathsep.join((str(Path(git).parent), str(system / "System32")))
            env.update(
                {
                    key: os.environ[key]
                    for key in ("SystemRoot", "WINDIR", "TEMP", "TMP")
                    if key in os.environ
                }
            )
            env["GIT_CONFIG_COUNT"] = "2"
            env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"] = "core.autocrlf", "false"
            env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"] = "core.filemode", "false"
        for args in (
            ["init", "--template="],
            ["add", "--all", "--force"],
            ["-c", f"core.hooksPath={os.devnull}", "commit", "--allow-empty", "-m", "Baseline"],
        ):
            subprocess.run(
                [git, *args],
                cwd=self.workspace,
                env=env,
                capture_output=True,
                check=True,
                timeout=30,
            )
        for directory, _, names in os.walk(self.workspace / ".git"):
            Path(directory).chmod(0o777)
            for name in names:
                (Path(directory) / name).chmod(0o666)

    @classmethod
    def review(cls, directory: str | Path):
        self = cls.__new__(cls)
        self.directory = Path(directory).resolve(strict=True)
        state = json.loads((self.directory / "state.json").read_text())
        self.root = Path(state["root"]).resolve(strict=True)
        self.workspace = self.directory / "workspace"
        self.policy = SandboxPolicy()
        self.guard = WritebackGuard()
        self.guard.needs_review = True
        self.last_backup = None
        self.baseline = state["baseline"]
        self.protected = set(state["protected"])
        return self

    @classmethod
    def resume(cls, directory, root, policy, *, verify_command=None, backend=None):
        """Reconnect to the original copy; never silently discard unpublished changes."""
        try:
            self = cls.review(directory)
            if self.root != Path(root).resolve(strict=True):
                raise ValueError("沙箱属于其他项目")
            if self.workspace.is_symlink() or not self.workspace.is_dir():
                raise ValueError("原工作副本不存在或不是普通目录")
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError(
                "无法恢复上次会话的沙箱副本；请恢复原副本，或使用 --new-session 开始新会话"
            ) from None
        self.policy = policy
        for target in runtime_protected_paths(self.root):
            if target.is_relative_to(self.root):
                relative = target.relative_to(self.root).as_posix()
                self.protected.add(relative + "/" if target.is_dir() else relative)
        self.backend = backend or DockerBackend(policy)
        self.verify_command = list(verify_command) if verify_command else None
        # review() marks unpublished changes for review. begin_task() clears that
        # marker only if the copy is clean; restarting must not erase failed checks.
        return self

    def _save(self):
        atomic_json(
            self.directory / "state.json",
            {
                "root": str(self.root),
                "baseline": self.baseline,
                "protected": sorted(self.protected),
            },
        )

    def begin_task(self):
        """Carry unresolved failures only while there are unpublished file changes."""
        if getattr(self.backend, "healthy", True) and not self.changes()[1]:
            self.guard = WritebackGuard()

    def verify_writeback(self):
        """Run the host-configured final check in the container, never via model arguments."""
        if not self.verify_command:
            return None
        try:
            result = self.backend.execute(
                self.workspace,
                "run_command",
                {
                    "command": self.verify_command,
                    "cwd": ".",
                    "timeout_seconds": self.policy.command_timeout_seconds,
                },
            )
            atomic_json(
                self.directory / "verification.json",
                {
                    "command": self.verify_command,
                    "success": result.success,
                    "data": result.data,
                    "error_code": result.error_code,
                    "error": result.error,
                },
            )
            guard = WritebackGuard()
            guard.record("run_command", {"command": self.verify_command}, result)
            if guard.needs_review or not getattr(self.backend, "healthy", True):
                self.guard.needs_review = True
            if guard.pending or self.guard.needs_review:
                return "最终验证未通过：" + (", ".join(guard.pending.values()) or "容器清理未确认")
            return None
        except BaseException:
            self.guard.needs_review = True
            raise

    def tools(self, *, writeback_mode=None):
        if writeback_mode not in {None, "manual", "on-success"}:
            raise ValueError("writeback_mode must be manual or on-success")
        return [
            SandboxedTool(
                tool.definition,
                self,
                execution_kind=execution_kind_of(tool),
                scheduling_policy=scheduling_policy_of(tool),
                writeback_mode=writeback_mode,
            )
            for tool in create_default_tools(
                self.workspace,
                isolated_execution=True,
                command_timeout_seconds=self.policy.command_timeout_seconds,
                python_timeout_seconds=self.policy.python_timeout_seconds,
            )
        ]

    def changes(self):
        current = files(self.workspace, self.policy, strict=True, protected=self.protected)
        changed = [
            name
            for name in sorted(self.baseline.keys() | current.keys())
            if self.baseline.get(name) != (fingerprint(*current[name]) if name in current else None)
        ]
        return current, changed

    def diff(self) -> str:
        current, changed = self.changes()
        output = []
        host = files(self.root, self.policy, protected=self.protected)
        for name in changed:
            status = (
                "M"
                if name in self.baseline and name in current
                else ("A" if name in current else "D")
            )
            output.append(f"{status} {name}")
            # Host content is for review only; apply separately verifies the baseline.
            before = host.get(name, (b"", 0))[0][: 128 * 1024]
            after = current.get(name, (b"", 0))[0][: 128 * 1024]
            output.extend(
                difflib.unified_diff(
                    before.decode("utf-8", "replace").splitlines(),
                    after.decode("utf-8", "replace").splitlines(),
                    fromfile="host/" + name,
                    tofile="sandbox/" + name,
                    lineterm="",
                )
            )
        return "\n".join(output)[:200_000] or "没有待回写的文件变更。"

    def apply(self) -> list[str]:
        if not getattr(getattr(self, "backend", None), "healthy", True):
            raise ValueError("容器清理未确认，拒绝回写。")
        self.guard.needs_review = True
        self.last_backup = None
        current, changed = self.changes()
        result = self._apply_snapshot(current, changed, self.baseline)
        self.guard = WritebackGuard()
        return result

    def _apply_snapshot(self, current, changed, expected, *, preserve_mode=False):
        if os.name == "nt":
            from host_support.windows_files import validate_snapshot_names

            validate_snapshot_names(set(current) | set(changed) | set(expected))
        host = files(self.root, self.policy, protected=self.protected)
        for name in changed:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts or self.policy.excluded(name):
                raise ValueError(f"非法回写路径：{name}")
            target = self.root
            for part in parts:
                target = target / part
                if target.is_symlink() or (
                    os.name == "nt"
                    and target.exists()
                    and target.lstat().st_file_attributes & 0x400
                ):
                    raise ValueError(f"目标包含符号链接：{name}")
            if target.exists():
                info = target.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(f"目标不是独立普通文件：{name}")
            actual = fingerprint(*host[name]) if name in host else None
            if actual != expected.get(name):
                raise ValueError(f"原项目已发生变化，未回写：{name}")
        if not changed:
            return []
        backup = Backup(self, host, current, changed)
        self.last_backup = backup.directory
        # Directory descriptors prevent symlink traversal during writeback.
        for name in changed:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts or self.policy.excluded(name):
                raise ValueError(f"非法回写路径：{name}")
            fd = open_directory(self.root)
            try:
                for part in parts[:-1]:
                    try:
                        mkdir_at(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = open_directory(part, dir_fd=fd)
                    os.close(fd)
                    fd = child
                try:
                    source = open_file(parts[-1], dir_fd=fd)
                except FileNotFoundError:
                    actual = None
                else:
                    with os.fdopen(source, "rb") as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError(f"不安全的目标：{name}")
                        actual = fingerprint(
                            stream.read(self.policy.max_workspace_bytes + 1), info.st_mode
                        )
                if actual != expected.get(name):
                    raise ValueError(f"回写期间文件改变：{name}；之前的回写可能已完成。")
                backup.attempting(name)
                if name not in current:
                    unlink_at(parts[-1], dir_fd=fd)
                else:
                    import uuid

                    temporary = ".sandbox-" + uuid.uuid4().hex
                    data, mode = current[name]
                    out = open_file(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd
                    )
                    try:
                        with os.fdopen(out, "wb") as stream:
                            stream.write(data)
                            permissions = (
                                mode & 0o777
                                if preserve_mode
                                else (
                                    (host.get(name, (b"", 0o644))[1] & 0o666)
                                    | (0o111 if mode & 0o111 else 0)
                                )
                            )
                            set_file_mode(stream.fileno(), permissions)
                            stream.flush()
                            os.fsync(stream.fileno())
                        rename_at(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, replace=True)
                    finally:
                        try:
                            unlink_at(temporary, dir_fd=fd)
                        except FileNotFoundError:
                            pass
                if name in current:
                    self.baseline[name] = fingerprint(*current[name])
                else:
                    self.baseline.pop(name, None)
                self._save()
            finally:
                os.close(fd)
        backup.complete()
        return changed

    def restore(self, backup_id: str) -> list[str]:
        """Restore only attempted writes, refusing to overwrite subsequent user edits."""
        if len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
            raise ValueError("备份 ID 必须是 backups 目录下的 32 位名称。")
        directory = self.directory / "backups" / backup_id
        record = json.loads((directory / "manifest.json").read_text())
        if os.name == "nt":
            from host_support.windows_files import validate_snapshot_names

            validate_snapshot_names(record["files"])
        if record["root"] != str(self.root):
            raise ValueError("备份与当前项目不匹配。")
        host = files(self.root, self.policy, protected=self.protected)
        desired, expected, changed = {}, {}, []
        already_restored = {}
        for name, entry in record["files"].items():
            if not entry["attempted"]:
                continue
            # Check paths even when a missing file would otherwise look restored.
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or self.policy.excluded(name)
                or any(
                    name == item.rstrip("/") or name.startswith(item.rstrip("/") + "/")
                    for item in self.protected
                )
            ):
                raise ValueError("备份包含受保护或非法路径。")
            target = self.root
            for part in Path(name).parts:
                target = target / part
                if target.is_symlink() or (
                    os.name == "nt"
                    and target.exists()
                    and target.lstat().st_file_attributes & 0x400
                ):
                    raise ValueError(f"恢复路径包含符号链接：{name}")
            actual = fingerprint(*host[name]) if name in host else None
            if actual == entry["before"]:
                already_restored[name] = actual
                continue
            if actual != entry["after"]:
                raise ValueError(f"回写后文件被修改，拒绝恢复：{name}")
            expected[name] = entry["after"]
            changed.append(name)
            if entry["blob"] is not None:
                if not str(entry["blob"]).isdigit():
                    raise ValueError("无效备份文件。")
                blob = directory / entry["blob"]
                descriptor = open_file(blob)
                with os.fdopen(descriptor, "rb") as source:
                    data = source.read(self.policy.max_workspace_bytes + 1)
                if fingerprint(data, entry["mode"]) != entry["before"]:
                    raise ValueError("备份内容校验失败。")
                desired[name] = (data, entry["mode"])
        self.guard.needs_review = True
        result = self._apply_snapshot(desired, changed, expected, preserve_mode=True)
        for name, actual in already_restored.items():
            if actual is None:
                self.baseline.pop(name, None)
            else:
                self.baseline[name] = actual
        record["status"] = "restored"
        atomic_json(directory / "manifest.json", record)
        self._save()
        return result
