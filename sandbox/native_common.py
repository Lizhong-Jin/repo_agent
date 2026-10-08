"""Platform-selected native execution against the workspace; never falls back."""

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from contextvars import ContextVar
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter

from tools._internal.base import ExecutionKind, ToolEffects, ToolResult, execution_kind_of
from tools._internal.file_access import FileAccess
from tools._internal.file_policy import runtime_protected_paths
from tools._internal.process_runner import ProcessRunner, _BoundedCapture
from tools.execute import (
    PROCESS_EXECUTION_TOOLS,
    GetExecutionEnvironmentTool,
    RunCommandTool,
    RunPythonTool,
    RunShellTool,
)
from tools.factory import create_default_tools, create_file_tools
from tools.scheduling import SERIAL, scheduling_policy_of

from .concurrency import backend_gate
from .project_python import select_python

_call_metrics = ContextVar("native_call_metrics", default=None)


class NativeTool:
    def __init__(self, definition, backend, execution_kind, *, scheduling_policy=SERIAL):
        self.definition = definition
        self.backend = backend
        self.execution_kind = execution_kind
        self.scheduling_policy = scheduling_policy
        if execution_kind_of(self) not in {
            ExecutionKind.TRUSTED_FILE,
            ExecutionKind.SANDBOXED_PROCESS,
        }:
            raise ValueError("Native proxies only accept file/process tools")

    def execute(self, arguments):
        return self.backend.execute(self.backend.workspace, self.definition.name, arguments)


class _NativeCommandRunner:
    """Validated commands are launched by the host, always inside the OS sandbox."""

    def __init__(self, backend):
        self.backend = backend

    def run(self, command, *, cwd, timeout_seconds):
        return self.backend._run(
            command, cwd=cwd, timeout=timeout_seconds, max_output_bytes=32 * 1024, project=True
        )


class NativeBackendBase:
    """Shared lifecycle; platform policy and enforcement live in concrete backends."""

    platform_name = "unsupported"
    isolation = None
    temporary_root = None
    command_timeout_seconds = 120
    python_timeout_seconds = 30
    requested_profile = "auto"

    RECOVERY_READ_TOOLS = frozenset(
        {
            "read_file",
            "list_files",
            "find_files",
            "search_files",
            "get_path_info",
        }
    )

    def _platform_setup(self):
        raise NotImplementedError("A native platform backend is required")

    def _read_paths(self):
        raise NotImplementedError("A native platform backend is required")

    def _environment(self, scratch):
        raise NotImplementedError("A native platform backend is required")

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        raise NotImplementedError("A native platform backend is required")

    def _preflight(self):
        raise NotImplementedError("A native platform backend is required")

    def __init__(
        self, workspace, *, profile="auto", gpus=None, project_python=None, isolated_workspace=False
    ):
        initialization_started = perf_counter()
        self.startup_metrics = {}
        self._performance_runs = []
        if profile not in {"auto", "standard", "cuda", "metal"}:
            raise ValueError("profile must be auto, standard, cuda or metal")
        if profile == "metal" and self.platform_name != "macos":
            raise ValueError("Metal 仅支持 Apple Silicon macOS native")
        if profile == "standard" and gpus is not None:
            raise ValueError("GPU selection requires the cuda profile")
        if (profile == "cuda" or gpus is not None) and self.platform_name != "linux":
            raise ValueError("原生 GPU 的 cuda profile / GPU 选择仅支持 Linux / WSL2 NVIDIA CUDA")
        self.requested_profile = profile
        self.requested_gpus = gpus
        self._platform_setup()
        self.workspace = Path(workspace).resolve(strict=True)
        self.isolated_workspace = isolated_workspace
        if isolated_workspace and project_python:
            candidate = Path(project_python).expanduser()
            candidate = candidate if candidate.is_absolute() else self.workspace / candidate
            if not Path(os.path.abspath(candidate)).is_relative_to(self.workspace):
                raise ValueError("独立工作区的 --project-python 必须位于该工作区内")
        if not self.workspace.is_dir():
            raise ValueError("Native 工作区必须是目录")
        self.healthy = True
        self.last_cleanup = {}
        self._temporary = tempfile.TemporaryDirectory(
            prefix="repo-agent-native-", dir=self.temporary_root
        )
        self.directory = Path(self._temporary.name).resolve()
        try:
            if self.directory.is_relative_to(self.workspace):
                raise ValueError("Native 工作区不能包含沙箱控制目录；请选择具体项目目录")
            self.runtime = self.directory / "runtime"
            from .resources import trusted_code_root

            source = trusted_code_root()
            copy_started = perf_counter()
            # Freeze trusted tool code before allowing edits to the agent's own repository.
            for package in ("tools", "llm", "sandbox", "host_support"):
                shutil.copytree(
                    source / package,
                    self.runtime / package,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            self.startup_metrics["runtime_copy_ms"] = (perf_counter() - copy_started) * 1000
            self.python = Path(sys.executable).absolute()
            self.read_paths = self._read_paths()
            self.protected_paths = tuple(runtime_protected_paths(self.workspace))
            self._prepare_workspace()
            self.startup_metrics["workspace_check_ms"] = self.last_workspace_check_ms
            self._preflight()
            self.project_python = select_python(
                self.workspace,
                project_python,
                agent_python=self.python,
                trusted_paths=self.read_paths,
                environment={"PATH": os.devnull} if isolated_workspace else None,
            )
            if isolated_workspace and self.project_python.source != "agent fallback":
                parent = self.project_python.executable.parent
                if parent.resolve() != parent.absolute():
                    raise ValueError("独立工作区不能通过目录符号链接复用外部 Python 环境")
            self.project_python_info = self._probe_project_python()
            if hasattr(self, "preflight_metrics"):
                self.startup_metrics["checks"] = self.preflight_metrics
        except BaseException:
            self.close()
            raise
        finally:
            self.startup_metrics["total_ms"] = (perf_counter() - initialization_started) * 1000
            self.startup_metrics["runs"] = self._performance_runs
            del self._performance_runs

    def _prepare_workspace(self):
        """macOS validates here; Linux validates during each mount-policy scan."""
        self._measure_workspace_check()

    def _measure_workspace_check(self):
        started = perf_counter()
        try:
            self._check_workspace()
        finally:
            self._set_metric("last_workspace_check_ms", (perf_counter() - started) * 1000)

    def _check_workspace(self):
        # A pre-existing hard link would let an allowed path modify an outside inode.
        # Creating new hard links is separately blocked by Seatbelt's file-link rule.
        def inaccessible(error):
            raise error

        for directory, _, names in os.walk(self.workspace, followlinks=False, onerror=inaccessible):
            for name in names:
                path = os.path.join(directory, name)
                self._check_workspace_file(path, os.lstat(path))

    def _check_workspace_file(self, path: str, info: os.stat_result):
        """Platform checks share one fresh lstat result per non-directory entry."""
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError(f"Native 工作区含硬链接，拒绝执行：{path}")

    def execution_context(self):
        return {
            "mode": "native",
            "platform": self.platform_name,
            "isolation": self.isolation,
            "network": "disabled",
            "changes_apply_to": "independent_workspace"
            if getattr(self, "isolated_workspace", False)
            else "original_project",
            "workspace_kind": "worktree"
            if getattr(self, "isolated_workspace", False)
            else "direct",
            "writeback_mode": "direct",
            "file_tools": "trusted_host_file_service",
            "process_tools": "os_sandbox",
            "writable_paths": [str(self.workspace), "per-call temporary directory"],
            "python_environments": {
                "agent": str(self.python),
                "project": str(self.project_python.executable)
                if getattr(self, "project_python", None)
                else str(self.python),
                "source": self.project_python.source
                if getattr(self, "project_python", None)
                else "agent fallback",
                "project_info": getattr(self, "project_python_info", {}),
            },
            "resources": {"memory_limit": None, "cpu_limit": None, "pids_limit": None},
            "persistence": {
                "workspace_files_across_calls": True,
                "tmp_across_calls": False,
                "process_cleanup": "host_supervised_pid_and_start_time_best_effort",
            },
        }

    def _probe_project_python(self):
        selected = self.project_python
        if selected.executable == self.python:
            return {"executable": str(self.python), "version": sys.version.split()[0]}
        result = self._run(
            [
                str(selected.executable),
                "-I",
                "-c",
                'import json,sys; print(json.dumps({"executable":sys.executable,'
                '"version":sys.version.split()[0],"prefix":sys.prefix}))',
            ],
            timeout=20,
            project=True,
        )
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated or not self.healthy:
            raise ValueError("项目 Python 沙箱内自检失败；不会切换解释器：" + result.stderr[:2000])
        try:
            report = json.loads(result.stdout)
            if not isinstance(report, dict) or not isinstance(report.get("version"), str):
                raise ValueError("invalid report")
        except (ValueError, TypeError) as error:
            raise ValueError("项目 Python 返回无效自检信息") from error
        return report

    def _run(
        self,
        command=None,
        *,
        request=None,
        git_read=False,
        timeout=130,
        cwd=None,
        max_output_bytes=4 * 1024 * 1024,
        project=False,
    ):
        started = perf_counter()
        metrics = {"preparation_ms": 0.0, "process_ms": 0.0, "total_ms": 0.0, "complete": False}
        try:
            result = self._run_measured(
                command,
                request=request,
                git_read=git_read,
                timeout=timeout,
                cwd=cwd,
                max_output_bytes=max_output_bytes,
                metrics=metrics,
                project=project,
            )
            metrics["complete"] = True
            return result
        finally:
            metrics["total_ms"] = (perf_counter() - started) * 1000
            self._set_metric("last_run_metrics", metrics)
            active = self._active_metrics()
            if active is not None:
                active["runs"].append(metrics)
            elif hasattr(self, "_performance_runs"):
                self._performance_runs.append(metrics)

    def _run_measured(
        self, command, *, request, git_read, timeout, cwd, max_output_bytes, metrics, project=False
    ):
        preparation_started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="call-", dir=self.directory) as call:
            control = Path(call)
            scratch = control / "scratch"
            scratch.mkdir()
            read_paths = self.read_paths
            project_environment = getattr(self, "project_python", None)
            if project_environment:
                # Embedded environments are already visible through the workspace;
                # protect them from writes even during control-only operations.
                read_paths = tuple(
                    dict.fromkeys(
                        (
                            *read_paths,
                            *(
                                path
                                for path in project_environment.read_paths
                                if path.is_relative_to(self.workspace)
                            ),
                        )
                    )
                )
            selected = project_environment if project else None
            if selected:
                read_paths = tuple(dict.fromkeys((*read_paths, *selected.read_paths)))
            aliases = None
            if selected and request is None:
                aliases = control / "python-bin"
                aliases.mkdir()
                for name in ("python", "python3"):
                    path = aliases / name
                    path.write_text(
                        "#!/bin/sh\nexec " + shlex.quote(str(selected.executable)) + ' "$@"\n'
                    )
                    path.chmod(0o555)
                read_paths = (*read_paths, aliases)
            if request is not None:
                request_path = control / "request.json"
                request_path.write_text(json.dumps(request))
                read_paths = (*read_paths, request_path)
                bootstrap = (
                    "import sys; "
                    f"sys.path.insert(0, {str(self.runtime)!r}); "
                    "from sandbox.worker import execute_request; import json; "
                    f"execute_request(json.load(open({str(request_path)!r})), "
                    f"{str(self.workspace)!r})"
                )
                command = [str(self.python), "-I", "-c", bootstrap]
            self._set_metric("last_policy_metrics", None)
            try:
                invocation = self._sandbox_command(
                    command, control, scratch, read_paths, git_read=git_read
                )
            finally:
                metrics["preparation_ms"] = (perf_counter() - preparation_started) * 1000
                policy_metrics = self._get_metric("last_policy_metrics")
                if policy_metrics is not None:
                    metrics["policy"] = dict(policy_metrics)
            environment = self._environment(scratch)
            # Worker/control executables retain trusted PATH. Only user commands
            # receive the project environment's command search path.
            if selected and request is None:
                environment["PATH"] = os.pathsep.join(
                    [str(aliases), str(selected.executable.parent), environment["PATH"]]
                )
                prefix = selected.executable.parent.parent
                if (prefix / "conda-meta").is_dir():
                    environment["CONDA_PREFIX"] = str(prefix)
                elif (prefix / "pyvenv.cfg").is_file():
                    environment["VIRTUAL_ENV"] = str(prefix)
            runner = ProcessRunner(
                max_output_bytes=max_output_bytes, base_env=environment, supervise_tree=True
            )
            process_started = perf_counter()
            try:
                result = runner.run(
                    invocation,
                    cwd=cwd or self.workspace,
                    timeout_seconds=timeout,
                )
            except BaseException:
                if runner.last_cleanup_status == "unknown":
                    self.healthy = False
                raise
            finally:
                metrics["process_ms"] = (perf_counter() - process_started) * 1000
            if result.cleanup_error or (result.timed_out and result.cleanup_status != "confirmed"):
                self.healthy = False
                self.last_cleanup = {
                    "cleanup_status": result.cleanup_status,
                    "cleanup_error": result.cleanup_error,
                    "cleanup_diagnostics": result.cleanup_diagnostics,
                    "pid": result.pid,
                    "process_group_id": result.process_group_id,
                }
            return result

    def tools(self):
        return [
            NativeTool(
                tool.definition,
                self,
                execution_kind_of(tool),
                scheduling_policy=scheduling_policy_of(tool)
                if execution_kind_of(tool) is ExecutionKind.TRUSTED_FILE
                else SERIAL,
            )
            for tool in self._tool_catalog().values()
        ]

    def _tool_catalog(self):
        with backend_gate(self).metadata:
            if not hasattr(self, "_native_tools"):
                self._native_tools = {
                    tool.definition.name: tool
                    for tool in create_default_tools(
                        self.workspace,
                        isolated_execution=True,
                        **self._tool_limits(),
                    )
                }
            return self._native_tools

    def _file_tools(self):
        with backend_gate(self).metadata:
            # Only factory-owned built-ins are eligible; a plugin's self-declared
            # attribute or model argument cannot grant host execution privileges.
            if not hasattr(self, "_trusted_file_tools"):
                self._trusted_file_tools = {
                    tool.definition.name: tool
                    for tool in create_file_tools(self.workspace)
                    if execution_kind_of(tool) is ExecutionKind.TRUSTED_FILE
                }
            return self._trusted_file_tools

    def _execute_file(self, tool, arguments):
        protected = tuple(getattr(self, "protected_paths", ())) + tuple(
            runtime_protected_paths(self.workspace)
        )
        selected = getattr(self, "project_python", None)
        readonly = (*self.read_paths, *(selected.read_paths if selected else ()))
        access = FileAccess(
            self.workspace,
            protected_paths=protected,
            read_only_paths=readonly,
            directory_backend=getattr(self, "directory_backend", None),
        )
        try:
            with access.activate():
                result = tool.execute(arguments)
        except OSError:
            result = ToolResult(
                False,
                error_code="PERMISSION_DENIED",
                error="无法安全访问工作区；请检查目录及文件权限",
            )
        return replace(result, data={**result.data, "execution_allowed": self.healthy})

    def _tool_limits(self):
        return {
            "command_timeout_seconds": self.command_timeout_seconds,
            "python_timeout_seconds": self.python_timeout_seconds,
        }

    def _active_metrics(self):
        active = _call_metrics.get()
        return active[1] if active is not None and active[0] is self else None

    def _set_metric(self, name, value):
        active = self._active_metrics()
        if active is None:
            setattr(self, name, value)
        else:
            active[name] = value

    def _get_metric(self, name):
        active = self._active_metrics()
        return active.get(name) if active is not None else getattr(self, name, None)

    def execute(self, workspace, name, arguments):
        tool = self._tool_catalog().get(name)
        read = (
            tool is not None
            and execution_kind_of(tool) is ExecutionKind.TRUSTED_FILE
            and scheduling_policy_of(tool).parallel
        )
        with backend_gate(self).hold(read=read):
            started = perf_counter()
            metrics = {"last_workspace_check_ms": 0.0, "runs": []}
            token = _call_metrics.set((self, metrics))
            try:
                return self._execute(workspace, name, arguments)
            finally:
                _call_metrics.reset(token)
                snapshot = {
                    "tool": name,
                    "total_ms": (perf_counter() - started) * 1000,
                    "workspace_check_ms": metrics["last_workspace_check_ms"],
                    "runs": metrics.pop("runs"),
                }
                # Backwards-compatible last-completed diagnostics. Execution never
                # reads this shared snapshot while inside a call's metrics scope.
                with backend_gate(self).metadata:
                    self.__dict__.update(metrics, last_tool_metrics=snapshot)

    def _execute(self, workspace, name, arguments):
        if Path(workspace).resolve() != self.workspace:
            raise ValueError("Native 后端不能切换工作区")
        tool = self._tool_catalog().get(name)
        if tool is None:
            return ToolResult(False, error_code="UNKNOWN_TOOL", error=f"Unknown tool: {name}")
        kind = execution_kind_of(tool)
        if kind not in {ExecutionKind.TRUSTED_FILE, ExecutionKind.SANDBOXED_PROCESS}:
            raise ValueError(f"Native backend cannot execute host tool: {name}")
        if not self.healthy:
            if name == "get_execution_environment":
                # Same machine, but no runtime/GPU probe processes while degraded.
                result = GetExecutionEnvironmentTool(self.workspace).execute(arguments)
                if result.success:
                    if "execution" in result.data:
                        result.data["execution"].update(self.execution_context())
                        result.data["execution"]["command_execution_allowed"] = False
                        result.data["execution"]["python_execution_allowed"] = False
                    result.data["executor_healthy"] = False
                    result.data["last_cleanup"] = getattr(self, "last_cleanup", {})
                return result
            if name not in self.RECOVERY_READ_TOOLS:
                return ToolResult(
                    False,
                    error_code="NATIVE_UNHEALTHY",
                    error="进程清理未确认，暂停执行和写入；仍可读取文件和查询环境状态",
                    data={
                        "execution_allowed": False,
                        "last_cleanup": getattr(self, "last_cleanup", {}),
                    },
                )
        if kind is ExecutionKind.TRUSTED_FILE:
            file_tool = self._file_tools().get(name)
            if file_tool is None:
                raise ValueError(
                    f"Native file tool must be registered by create_file_tools: {name}"
                )
            return self._execute_file(file_tool, arguments)
        self._prepare_workspace()
        if name in PROCESS_EXECUTION_TOOLS:
            # Keep argument/path validation identical to worker tools, but collect
            # program output directly outside the worker's final JSON protocol.
            tool = (
                {"run_command": RunCommandTool, "run_shell": RunShellTool}[name](
                    self.workspace,
                    execution_allowed=True,
                    default_timeout_seconds=min(60, self.command_timeout_seconds),
                    max_timeout_seconds=self.command_timeout_seconds,
                )
                if name != "run_python"
                else RunPythonTool(
                    self.workspace,
                    execution_allowed=True,
                    python_executable=(
                        self.project_python.executable
                        if getattr(self, "project_python", None)
                        else self.python
                    ),
                    max_timeout_seconds=self.python_timeout_seconds,
                )
            )
            tool.runner = _NativeCommandRunner(self)
            result = tool.execute(arguments)
            if result.data.get("cleanup_error"):
                return ToolResult(
                    False,
                    error_code="NATIVE_EXECUTION_FAILED",
                    error="命令已返回，但进程清理未确认；保留已收集的输出",
                    data={**result.data, "execution_allowed": self.healthy},
                    effects=result.effects,
                )
            return replace(result, data={**result.data, "execution_allowed": self.healthy})
        request = {
            "name": name,
            "arguments": arguments,
            "execution_context": self.execution_context(),
            "tool_limits": self._tool_limits(),
        }
        # -I excludes the workspace/PYTHONPATH; trusted package copies take precedence.
        result = self._run(
            request=request,
            git_read=name in {"git_status", "git_diff", "git_log", "git_show"},
            project=name not in {"git_status", "git_diff", "git_log", "git_show"},
        )
        if (
            result.exit_code != 0
            or result.timed_out
            or result.stdout_truncated
            or result.cleanup_error
        ):
            return ToolResult(
                False,
                error_code="NATIVE_EXECUTION_FAILED",
                error="原生沙箱执行失败、超时或输出超过限制；文件修改可能已经生效",
                data=self._failure_data(result),
                effects=ToolEffects.process(asdict(result)),
            )
        try:
            payload = json.loads(result.stdout)
            tool_result = ToolResult(**payload)
        except (ValueError, TypeError):
            return ToolResult(
                False,
                error_code="NATIVE_PROTOCOL_ERROR",
                error="原生 worker 返回无效结果",
                data=self._failure_data(result),
                effects=ToolEffects.process(asdict(result)),
            )
        if (
            tool_result.data.get("cleanup_error")
            or tool_result.data.get("timed_out")
            or tool_result.error_code == "LSP_TIMEOUT"
        ):
            if result.cleanup_status == "confirmed":
                data = dict(tool_result.data)
                data["inner_cleanup_error"] = data.get("cleanup_error")
                data.update(
                    cleanup_error=None,
                    cleanup_status="confirmed",
                    cleanup_diagnostics=result.cleanup_diagnostics,
                )
                tool_result = replace(tool_result, data=data, effects=ToolEffects.process(data))
            else:
                self.healthy = False
        return replace(tool_result, data={**tool_result.data, "execution_allowed": self.healthy})

    def _failure_data(self, result):
        data = asdict(result)
        for name in ("stdout", "stderr"):
            capture = _BoundedCapture(32 * 1024)
            capture.feed(getattr(result, name).encode("utf-8"))
            data[name] = capture.text()
            data[name + "_truncated"] = data[name + "_truncated"] or capture.truncated
        data["output_complete"] = False
        data["output_kind"] = "worker_protocol"
        data["execution_allowed"] = self.healthy
        return data

    def close(self):
        self._temporary.cleanup()
