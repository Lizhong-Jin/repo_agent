"""Private workspace snapshots, conflict detection, and explicit writeback."""

from copy import deepcopy
from dataclasses import replace
import re
import difflib
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from tools.base import ToolResult
from tools.factory import create_default_tools

from .docker import DockerBackend, SandboxBackend
from .policy import SandboxPolicy
from .writeback import Backup, WritebackGuard, atomic_json


def fingerprint(data: bytes, mode: int) -> str:
    return hashlib.sha256(data).hexdigest() + (":x" if mode & 0o111 else ":-")


def files(
    root: Path, policy: SandboxPolicy, *, strict: bool = False, protected: set[str] | None = None
) -> dict[str, tuple[bytes, int]]:
    """Never follow links; refuse special files and hard links on export."""
    result = {}
    total = 0
    for directory, dirs, names, directory_fd in os.fwalk(root, follow_symlinks=False):
        for name in list(dirs) + names:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
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
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            with os.fdopen(descriptor, "rb") as source:
                current = os.fstat(source.fileno())
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise ValueError(f"文件类型改变：{relative}")
                data = source.read(policy.max_workspace_bytes - total + 1)
            total += len(data)
            if total > policy.max_workspace_bytes:
                raise ValueError("工作区超过 sandbox 大小限制。")
            result[relative] = (data, stat.S_IMODE(current.st_mode))
    return result


class SandboxedTool:
    def __init__(self, definition, session):
        if definition.name in {"run_command", "run_python"}:
            parameters = deepcopy(definition.parameters)
            parameters["properties"]["check_id"] = {
                "type": "string", "pattern": "^[a-zA-Z0-9_-]{1,64}$",
                "description": "Stable ID for one validation. Reuse on corrected retries of the same check; never reuse for unrelated checks.",
            }
            definition = replace(definition, parameters=parameters, description=definition.description +
                " For validation, supply a stable check_id from the first attempt. A successful retry "
                "with the same tool, cwd and check_id resolves its earlier failure even if code changes. "
                "Retain the original assertions; unrelated checks must use different IDs.")
        self.definition = definition
        self.session = session

    def execute(self, arguments: dict) -> ToolResult:
        try:
            execution_arguments = dict(arguments)
            if self.definition.name in {"run_command", "run_python"} and "check_id" in arguments:
                check_id = execution_arguments.pop("check_id")
                if not isinstance(check_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", check_id):
                    result = ToolResult(False, error_code="INVALID_ARGUMENTS", error="check_id must use 1-64 letters, digits, '_' or '-'.")
                    self.session.guard.record(self.definition.name, arguments, result)
                    return result
            result = self.session.backend.execute(
                self.session.workspace, self.definition.name, execution_arguments
            )
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
        for key in ("AGENT_ENV_FILE", "AGENT_LOG_DIR"):
            value = os.getenv(key)
            if value:
                target = (self.root / value).resolve()
                if target.is_relative_to(self.root):
                    relative = target.relative_to(self.root).as_posix()
                    if target.is_dir():
                        self.protected.add(relative + "/")
                    else:
                        self.protected.add(relative)
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
        for args in (
            ["init", "--template="],
            ["add", "--all", "--force"],
            ["-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-m", "Baseline"],
        ):
            subprocess.run(
                ["git", *args],
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
            result = self.backend.execute(self.workspace, "run_command", {
                "command": self.verify_command, "cwd": ".",
                "timeout_seconds": self.policy.command_timeout_seconds,
            })
            atomic_json(self.directory / "verification.json", {
                "command": self.verify_command, "success": result.success,
                "data": result.data, "error_code": result.error_code, "error": result.error,
            })
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

    def tools(self):
        return [
            SandboxedTool(tool.definition, self) for tool in create_default_tools(
                self.workspace, isolated_execution=True,
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
        host = files(self.root, self.policy, protected=self.protected)
        for name in changed:
            parts = Path(name).parts
            if Path(name).is_absolute() or ".." in parts or self.policy.excluded(name):
                raise ValueError(f"非法回写路径：{name}")
            target = self.root
            for part in parts:
                target = target / part
                if target.is_symlink():
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
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in parts[:-1]:
                    try:
                        os.mkdir(part, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                try:
                    source = os.open(
                        parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                    )
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
                    os.unlink(parts[-1], dir_fd=fd)
                else:
                    import uuid

                    temporary = ".sandbox-" + uuid.uuid4().hex
                    data, mode = current[name]
                    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
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
                            os.fchmod(stream.fileno(), permissions)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
                    finally:
                        try:
                            os.unlink(temporary, dir_fd=fd)
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
                if target.is_symlink():
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
                descriptor = os.open(blob, os.O_RDONLY | os.O_NOFOLLOW)
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
