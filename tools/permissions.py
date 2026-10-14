"""Host-owned, revocable grants. Model requests never change effective permissions.

Saved records are audit data, not authority: there is deliberately no restore API.
"""

import os
import re
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from uuid import uuid4

from llm import ToolDefinition

from ._internal.base import ExecutionKind, ToolEffects, ToolResult
from ._internal.file_access import FileAccess
from ._internal.file_policy import PathPolicy, runtime_protected_paths
from .scheduling import SERIAL


@dataclass(frozen=True)
class ReadOnlyRoot:
    id: str
    path: Path
    identity: tuple[int, int]

    def validate(self):
        info = self.path.lstat()
        if (
            self.path.resolve(strict=True) != self.path
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != self.identity
        ):
            raise ValueError("授权目录已被替换；请撤销后重新申请")


@dataclass(frozen=True)
class PermissionProfile:
    workspace: Path
    backend: str
    access_mode: str = "develop"
    read_only_roots: tuple[ReadOnlyRoot, ...] = ()
    host_discovery: bool = False


class PermissionManager:
    """One live session authority shared by UI, file service and native launch.

    Only trusted host composition and the idle user command handler call grant/
    decide/revoke. These methods are intentionally absent from model schemas.
    """

    def __init__(self, workspace, *, backend, access_mode="develop", protected_paths=()):
        if backend not in {"local", "native", "docker"} or access_mode not in {"develop", "review"}:
            raise ValueError("Invalid permission profile")
        self.profile = PermissionProfile(Path(workspace).resolve(strict=True), backend, access_mode)
        self.protected_paths = tuple(Path(p).resolve() for p in protected_paths)
        self._private_execution_paths = set()
        self.pending = {}
        self.events = []
        self.lock = RLock()
        # Capture the host search path, never consult model-supplied environment.
        self.search_path = tuple(os.environ.get("PATH", "").split(os.pathsep))

    def path_policy(self):
        return PathPolicy(
            protected_paths=(
                *self.protected_paths,
                *self._private_execution_paths,
                *runtime_protected_paths(self.profile.workspace),
            )
        )

    def protect_execution_path(self, path):
        """Remember live/retained call state for BOTH staging and file reads."""
        with self.lock:
            stale = set()
            for previous in self._private_execution_paths:
                try:
                    previous.lstat()
                except FileNotFoundError:
                    stale.add(previous)
                except OSError:
                    pass  # Inaccessible is still protected, not evidence of deletion.
            self._private_execution_paths.difference_update(stale)
            self._private_execution_paths.add(Path(path).resolve())

    def bind_backend(self, backend):
        """Compose all backend control-path exclusions before exposing tools."""
        if (
            self.profile.backend != "native"
            or Path(backend.workspace).resolve() != self.profile.workspace
        ):
            raise ValueError("Permission manager does not match the native workspace")
        with self.lock:
            protected = list(self.protected_paths)
            protected.extend(getattr(backend, "protected_paths", ()))
            for name in ("directory", "windows_state_root"):
                if (path := getattr(backend, name, None)) is not None:
                    protected.append(Path(path).resolve())
            self.protected_paths = tuple(dict.fromkeys(protected))
            for grant in self.profile.read_only_roots:
                self.root(grant.id)
            backend.permissions = self

    def _event(self, action, **fields):
        self.events = [*self.events[-127:], {"action": action, **fields}]

    def _validate_scope(self, capability, path):
        if capability == "external_read":
            if self.profile.backend != "native" or self.profile.access_mode == "review":
                raise ValueError("外部目录授权目前仅支持 develop + native")
            if not isinstance(path, str) or not path or len(path) > 4096:
                raise ValueError("必须提供外部目录的绝对路径")
            if not Path(path).is_absolute() or "\x00" in path:
                raise ValueError("必须提供外部目录的绝对路径")
        elif capability != "host_discovery" or path is not None:
            raise ValueError("仅支持 external_read 或不带 path 的 host_discovery")

    def request(self, capability, *, reason, path=None):
        self._validate_scope(capability, path)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("权限申请需要 1–1000 字符的原因")
        with self.lock:
            for request in self.pending.values():
                if (request["capability"], request["path"]) == (capability, path):
                    return dict(request)
            if len(self.pending) >= 8:
                raise ValueError("待处理申请过多；请先处理已有申请")
            identity = uuid4().hex[:12]
            request = dict(id=identity, capability=capability, path=path, reason=reason)
            self.pending[identity] = request
            self._event("request", **request)
            return dict(request)

    def grant(self, capability, path=None):
        self._validate_scope(capability, path)
        with self.lock:
            if capability == "host_discovery":
                self.profile = replace(self.profile, host_discovery=True)
                identity = "host_discovery"
            else:
                requested = Path(path)
                root = requested.resolve(strict=True)
                policy = self.path_policy()
                if (
                    not root.is_dir()
                    or root == Path(root.anchor)
                    or self.profile.workspace.is_relative_to(root)
                    or root.is_relative_to(self.profile.workspace)
                    or policy.protects_path(requested, root)
                ):
                    raise ValueError(
                        "授权目录必须在工作区外，且不能是根目录、工作区祖先或受保护路径"
                    )
                for grant in self.profile.read_only_roots:
                    if grant.path == root:
                        grant.validate()
                        return grant.id
                if len(self.profile.read_only_roots) >= 8:
                    raise ValueError("最多授权 8 个外部目录")
                info = root.lstat()
                grant = ReadOnlyRoot(uuid4().hex[:12], root, (info.st_dev, info.st_ino))
                grant.validate()
                self.profile = replace(
                    self.profile, read_only_roots=(*self.profile.read_only_roots, grant)
                )
                identity = grant.id
            self._event("grant", id=identity, capability=capability, path=path)
            return identity

    def decide(self, identity, *, approve):
        with self.lock:
            request = self.pending.get(identity)
            if request is None:
                raise ValueError("未知权限申请 ID；请先 /permissions 查看")
            if approve:
                self.grant(request["capability"], request["path"])
            del self.pending[identity]
            self._event("approve" if approve else "deny", id=identity)

    def revoke(self, identity):
        with self.lock:
            if identity == "all":
                self.profile = replace(self.profile, read_only_roots=(), host_discovery=False)
                self.pending.clear()
            elif identity == "host_discovery":
                self.profile = replace(self.profile, host_discovery=False)
            elif any(g.id == identity for g in self.profile.read_only_roots):
                self.profile = replace(
                    self.profile,
                    read_only_roots=tuple(
                        g for g in self.profile.read_only_roots if g.id != identity
                    ),
                )
            else:
                raise ValueError("未知授权 ID")
            self._event("revoke", id=identity)

    def reset(self):
        self.revoke("all")
        self.events = []

    def root(self, identity):
        for grant in self.profile.read_only_roots:
            if grant.id == identity:
                grant.validate()
                if self.path_policy().protects_path(grant.path, grant.path):
                    raise ValueError("授权目录现已属于受保护路径")
                return grant
        raise ValueError("未授权的外部目录；请使用 request_permission")

    def snapshot(self):
        with self.lock:
            return {
                "policy": "strict"
                if not self.profile.read_only_roots and not self.profile.host_discovery
                else "scoped",
                "backend": self.profile.backend,
                "access_mode": self.profile.access_mode,
                "workspace": str(self.profile.workspace),
                "workspace_write": self.profile.access_mode == "develop",
                "process_execution": self.profile.backend in {"native", "docker"}
                and self.profile.access_mode == "develop",
                "external_write": False,
                "process_network": "disabled" if self.profile.backend != "local" else "unavailable",
                "host_discovery": self.profile.host_discovery,
                "external_read": [
                    {"id": g.id, "path": str(g.path)} for g in self.profile.read_only_roots
                ],
                "pending": [dict(r) for r in self.pending.values()],
                "lifetime": "current live session only; never restored from snapshots",
                "execution_paths": "REPO_AGENT_READ_ROOTS JSON maps grant IDs "
                "to read-only per-call copies",
                "events": [dict(e) for e in self.events],
            }


class PermissionTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    scheduling_policy = SERIAL

    def __init__(self, manager, operation):
        self.manager, self.operation = manager, operation
        schemas = {
            "get_permissions": ({}, [], "Inspect effective permissions and pending requests."),
            "request_permission": (
                {
                    "capability": {"type": "string", "enum": ["external_read", "host_discovery"]},
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
                ["capability", "reason"],
                "Request permission from the user, then pause. This NEVER approves access. "
                "External paths must be absolute directories. "
                "No write, network or full-access grants.",
            ),
            "read_external": (
                {
                    "root_id": {"type": "string"},
                    "operation": {
                        "type": "string",
                        "enum": [
                            "read_file",
                            "list_files",
                            "find_files",
                            "search_files",
                            "get_path_info",
                        ],
                    },
                    "arguments": {"type": "object"},
                },
                ["root_id", "operation", "arguments"],
                "Read an explicitly authorized external directory using an existing file tool's "
                "arguments and paths relative to that root. No write operations. read_file uses "
                "reads:[{path:...}]; list_files uses path. Check get_permissions for root IDs.",
            ),
            "get_host_environment": (
                {"programs": {"type": "array", "maxItems": 32, "items": {"type": "string"}}},
                [],
                "With host_discovery permission, query host platform and executable locations. "
                "Never executes probes or exposes environment values/credentials. "
                "Finding a program "
                "does not authorize execution; use get_execution_environment for sandbox runtimes.",
            ),
        }
        properties, required, description = schemas[operation]
        self.definition = ToolDefinition(
            operation,
            description,
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )

    def execute(self, arguments):
        with self.manager.lock:
            return self._execute(arguments)

    def _execute(self, arguments):
        try:
            if self.operation == "get_permissions":
                data = self.manager.snapshot()
            elif self.operation == "request_permission":
                data = {"request": self.manager.request(**arguments), "approved": False}
            elif self.operation == "read_external":
                return self._read(arguments)
            else:
                data = self._host(arguments)
            return ToolResult(True, data, effects=ToolEffects("none"))
        except (OSError, ValueError, TypeError, KeyError):
            return ToolResult(
                False,
                error_code="PERMISSION_DENIED",
                error="权限或参数无效；请检查 get_permissions，并由用户明确授权。",
                effects=ToolEffects("none"),
            )

    def _read(self, arguments):
        from .factory import create_file_tools

        grant = self.manager.root(arguments["root_id"])
        operation = arguments["operation"]
        if operation not in {
            "read_file",
            "list_files",
            "find_files",
            "search_files",
            "get_path_info",
        }:
            raise ValueError("Only audited read operations are allowed")
        tools = create_file_tools(grant.path, tool_name=operation)
        # This descriptor context is read-only and includes the ORIGINAL workspace's
        # protected runtime paths. Do not nest a context that discards those paths.
        with FileAccess(
            grant.path,
            read_only_paths=(grant.path,),
            protected_paths=self.manager.path_policy().protected_paths,
        ).activate() as access:
            info = os.fstat(access.root_fd)
            if (info.st_dev, info.st_ino) != grant.identity:
                raise ValueError("授权目录已被替换")
            return tools[0].execute(arguments["arguments"])

    def _host(self, arguments):
        if not self.manager.profile.host_discovery:
            raise ValueError("host_discovery permission is required")
        programs = arguments.get(
            "programs", ["python", "git", "node", "docker", "podman", "sbatch"]
        )
        if (
            not isinstance(programs, list)
            or len(programs) > 32
            or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", p)
                for p in programs
            )
        ):
            raise ValueError("Use executable names, not paths or command lines")
        found = {}
        policy = self.manager.path_policy()
        for name in programs:
            found[name] = None
            for directory in self.manager.search_path[:256]:
                if not directory or not Path(directory).is_absolute():
                    continue
                for suffix in ("", ".exe", ".cmd", ".bat") if os.name == "nt" else ("",):
                    candidate = Path(directory) / (name + suffix)
                    try:
                        resolved = candidate.resolve(strict=True)
                        if (
                            resolved.is_relative_to(self.manager.profile.workspace)
                            or candidate.is_relative_to(self.manager.profile.workspace)
                            or policy.is_protected(candidate, resolved)
                        ):
                            continue
                        if resolved.is_file() and (os.name == "nt" or os.access(resolved, os.X_OK)):
                            found[name] = str(candidate)
                            break
                    except OSError:
                        continue
                if found[name]:
                    break
        return {
            "platform": sys.platform,
            "agent_python_version": sys.version.split()[0],
            "programs": found,
            "versions_probed": False,
            "execution_authorized": False,
        }


def permission_tools(manager):
    return [
        PermissionTool(manager, name)
        for name in (
            "get_permissions",
            "request_permission",
            "read_external",
            "get_host_environment",
        )
    ]
