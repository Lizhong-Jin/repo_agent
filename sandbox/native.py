"""Platform-selected native execution against the workspace; never falls back."""

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter

from tools._internal.base import ExecutionKind, ToolResult, execution_kind_of
from tools._internal.file_access import FileAccess
from tools._internal.file_policy import PROTECTED_NAMES, PROTECTED_SUFFIXES, runtime_protected_paths
from tools._internal.process_runner import ProcessRunner, _BoundedCapture
from tools.execute import GetExecutionEnvironmentTool, RunCommandTool, RunPythonTool
from tools.factory import create_default_tools, create_file_tools

from .project_python import select_python


def _quoted(value):
    text = str(value)
    if any(ord(char) < 32 for char in text):
        raise ValueError("Native 沙箱路径不能包含控制字符")
    return json.dumps(text, ensure_ascii=False)


def _pattern(text):
    # Seatbelt uses POSIX regexes, not Python's (?i) or non-capturing groups.
    return "".join(
        f"[{char.lower()}{char.upper()}]" if char.isalpha()
        else f"[{char}]" if char in ".-" else char
        for char in text
    )


def seatbelt_profile(workspace, scratch, read_paths, protected_paths, *, git_read=False):
    """Host-generated policy. Later deny rules also cover newly created names."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        # Never expose kern.procargs*: the host process environment contains keys.
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.") '
        '(sysctl-name "kern.ostype" "kern.osrelease" "kern.osversion" "kern.osproductversion" '
        '"kern.argmax" "kern.maxfiles" "kern.maxfilesperproc" "kern.maxproc" '
        '"kern.hostname" "kern.boottime" "kern.usrstack64" "kern.version"))',
        "(allow file-read-metadata)",
        # dyld/libignition opens / as an openat root during process startup.
        "(allow file-read* (literal \"/\"))",
        "(allow file-read* file-write-data (literal \"/dev/null\"))",
        "(allow file-read* (literal \"/dev/urandom\") (literal \"/dev/random\"))",
    ]
    for path in sorted(set(map(str, [workspace, scratch, *read_paths]))):
        lines.append(f"(allow file-read* file-map-executable (subpath {_quoted(path)}))")
    for path in (workspace, scratch):
        lines.append(f"(allow file-write* (subpath {_quoted(path)}))")
        # Do not rename/remove the allowed root itself.
        lines.append(f"(deny file-write-unlink (literal {_quoted(path)}))")
    # Protect the interpreter/dependencies even when installed inside the workspace.
    for path in read_paths:
        lines.append(f"(deny file-write* (subpath {_quoted(path)}))")
    names = "|".join(_pattern(name) for name in sorted(PROTECTED_NAMES - {".git"}))
    suffixes = "|".join(_pattern(suffix) for suffix in PROTECTED_SUFFIXES)
    pattern = f"(^|/)({names}|{_pattern('.env')}([.][^/]*)?|[^/]*({suffixes}))(/|$)"
    lines.append(f'(deny file-read* file-write* (regex #"{pattern}"))')
    git_ops = "file-write*" if git_read else "file-read* file-write*"
    lines.append(f'(deny {git_ops} (regex #"(^|/){_pattern(".git")}(/|$)"))')
    for path in protected_paths:
        lines.append(f"(deny file-read* file-write* (subpath {_quoted(path)}))")
    for path in (*protected_paths, *read_paths):
        # Ancestor renames must not relocate a protected subtree into an allowed path.
        for parent in Path(path).parents:
            if parent == workspace or not parent.is_relative_to(workspace):
                break
            lines.append(f"(deny file-write-unlink (literal {_quoted(parent)}))")
    # Deny hard-link creation even from otherwise readable toolchain paths.
    lines.extend(["(deny file-link)", "(deny network*)"])
    return "\n".join(lines) + "\n"


class NativeTool:
    def __init__(self, definition, backend, execution_kind):
        self.definition = definition
        self.backend = backend
        self.execution_kind = execution_kind
        if execution_kind_of(self) not in {
            ExecutionKind.TRUSTED_FILE, ExecutionKind.SANDBOXED_PROCESS,
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


class NativeBackend:
    """Shared lifecycle and macOS policy; Linux selects its specialized backend."""

    platform_name = "macos"
    isolation = "seatbelt"
    temporary_root = "/private/tmp"
    command_timeout_seconds = 120
    python_timeout_seconds = 30
    requested_profile = "auto"

    def __new__(cls, *args, **kwargs):
        if cls is NativeBackend and sys.platform == "linux":
            from .linux_native import LinuxNativeBackend

            return object.__new__(LinuxNativeBackend)
        return object.__new__(cls)

    def _platform_setup(self):
        if sys.platform != "darwin":
            raise ValueError("native 沙箱仅支持 macOS/Linux；不会退回未隔离执行")
        self.executable = Path("/usr/bin/sandbox-exec")
        if not self.executable.is_file():
            raise ValueError("未找到 macOS sandbox-exec；不会退回未隔离执行")

    RECOVERY_READ_TOOLS = frozenset({
        "read_file", "list_files", "find_files", "search_files", "get_path_info",
    })

    def __init__(self, workspace, *, profile="auto", gpus=None, project_python=None):
        initialization_started = perf_counter()
        self.startup_metrics = {}
        self._performance_runs = []
        if profile not in {"auto", "standard", "cuda"}:
            raise ValueError("profile must be auto, standard or cuda")
        if profile == "standard" and gpus is not None:
            raise ValueError("GPU selection requires the cuda profile")
        if (profile == "cuda" or gpus is not None) and self.platform_name != "linux":
            raise ValueError("原生 GPU 仅支持 Linux / WSL2 NVIDIA CUDA")
        self.requested_profile = profile
        self.requested_gpus = gpus
        self._platform_setup()
        self.workspace = Path(workspace).resolve(strict=True)
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
            for package in ("tools", "llm", "sandbox"):
                shutil.copytree(
                    source / package, self.runtime / package,
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
                self.workspace, project_python,
                agent_python=self.python, trusted_paths=self.read_paths)
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

    def _read_paths(self):
        paths = {
            self.runtime, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
            # Never allow /System wholesale: /System/Volumes/Data aliases user data.
            Path("/System/Library"), Path("/System/Cryptexes"),
            Path("/System/Volumes/Preboot/Cryptexes"),
            Path("/usr"), Path("/bin"), Path("/sbin"),
            Path("/Library/Apple"), Path("/Library/Developer"),
            Path("/Library/Frameworks"), Path("/opt/homebrew"),
            Path("/private/etc"), Path("/private/var/db/dyld"),
            Path("/private/var/db/timezone"),
        }
        # Include active dependency directories, but never sys.path's project entry.
        paths.update(
            Path(path).resolve() for path in sys.path
            if path and Path(path).name in {"site-packages", "dist-packages"}
        )
        return tuple(sorted(paths, key=str))

    def _prepare_workspace(self):
        """macOS validates here; Linux validates during each mount-policy scan."""
        self._measure_workspace_check()

    def _measure_workspace_check(self):
        started = perf_counter()
        try:
            self._check_workspace()
        finally:
            self.last_workspace_check_ms = (perf_counter() - started) * 1000

    def _check_workspace(self):
        # A pre-existing hard link would let an allowed path modify an outside inode.
        # Creating new hard links is separately blocked by Seatbelt's file-link rule.
        def inaccessible(error):
            raise error

        for directory, _, names in os.walk(
            self.workspace, followlinks=False, onerror=inaccessible
        ):
            for name in names:
                path = os.path.join(directory, name)
                self._check_workspace_file(path, os.lstat(path))

    def _check_workspace_file(self, path: str, info: os.stat_result):
        """Platform checks share one fresh lstat result per non-directory entry."""
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ValueError(f"Native 工作区含硬链接，拒绝执行：{path}")

    def execution_context(self):
        return {
            "mode": "native", "platform": self.platform_name, "isolation": self.isolation,
            "network": "disabled", "changes_apply_to": "original_project",
            "writeback_mode": "direct",
            "file_tools": "trusted_host_file_service",
            "process_tools": "os_sandbox",
            "writable_paths": [str(self.workspace), "per-call temporary directory"],
            "python_environments": {
                "agent": str(self.python),
                "project": str(self.project_python.executable)
                if getattr(self, 'project_python', None) else str(self.python),
                "source": self.project_python.source
                if getattr(self, 'project_python', None) else 'agent fallback',
                "project_info": getattr(self, 'project_python_info', {}),
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
            return {'executable': str(self.python), 'version': sys.version.split()[0]}
        result = self._run([str(selected.executable), '-I', '-c',
                           'import json,sys; print(json.dumps({"executable":sys.executable,'
                           '"version":sys.version.split()[0],"prefix":sys.prefix}))'],
                          timeout=20, project=True)
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated or not self.healthy:
            raise ValueError('项目 Python 沙箱内自检失败；不会切换解释器：' + result.stderr[:2000])
        try:
            report = json.loads(result.stdout)
            if not isinstance(report, dict) or not isinstance(report.get('version'), str):
                raise ValueError('invalid report')
        except (ValueError, TypeError) as error:
            raise ValueError('项目 Python 返回无效自检信息') from error
        return report

    def _environment(self, scratch):
        # Do not inherit API keys, proxy variables, agent sockets or startup hooks.
        bins = [
            str(self.python.parent), str(self.python.parent.parent / "lsp/node_modules/.bin"),
            "/opt/homebrew/opt/llvm/bin", "/usr/local/opt/llvm/bin",
            "/opt/homebrew/bin", "/usr/local/bin",
            "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ]
        return {
            "PATH": os.pathsep.join(bins), "HOME": str(scratch),
            "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "GOPATH": str(scratch / "go"), "GOCACHE": str(scratch / "go-build"),
            "GOMODCACHE": str(scratch / "go-mod"), "GOTOOLCHAIN": "local",
            "GOPROXY": "off", "GOSUMDB": "off", "GOTELEMETRY": "off",
        }

    def _run(self, command=None, *, request=None, git_read=False, timeout=130,
             cwd=None, max_output_bytes=4 * 1024 * 1024, project=False):
        started = perf_counter()
        metrics = {"preparation_ms": 0.0, "process_ms": 0.0, "total_ms": 0.0,
                   "complete": False}
        try:
            result = self._run_measured(
                command, request=request, git_read=git_read, timeout=timeout,
                cwd=cwd, max_output_bytes=max_output_bytes, metrics=metrics, project=project,
            )
            metrics["complete"] = True
            return result
        finally:
            metrics["total_ms"] = (perf_counter() - started) * 1000
            self.last_run_metrics = metrics
            if hasattr(self, "_performance_runs"):
                self._performance_runs.append(metrics)

    def _run_measured(self, command, *, request, git_read, timeout,
                      cwd, max_output_bytes, metrics, project=False):
        preparation_started = perf_counter()
        with tempfile.TemporaryDirectory(prefix="call-", dir=self.directory) as call:
            control = Path(call)
            scratch = control / "scratch"
            scratch.mkdir()
            read_paths = self.read_paths
            project_environment = getattr(self, 'project_python', None)
            if project_environment:
                # Embedded environments are already visible through the workspace;
                # protect them from writes even during control-only operations.
                read_paths = tuple(dict.fromkeys((*read_paths, *(
                    path for path in project_environment.read_paths
                    if path.is_relative_to(self.workspace)
                ))))
            selected = project_environment if project else None
            if selected:
                read_paths = tuple(dict.fromkeys((*read_paths, *selected.read_paths)))
            aliases = None
            if selected and request is None:
                aliases = control / 'python-bin'
                aliases.mkdir()
                for name in ('python', 'python3'):
                    path = aliases / name
                    path.write_text('#!/bin/sh\nexec ' + shlex.quote(str(selected.executable))
                                    + ' "$@"\n')
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
            self.last_policy_metrics = None
            try:
                invocation = self._sandbox_command(
                    command, control, scratch, read_paths, git_read=git_read
                )
            finally:
                metrics["preparation_ms"] = (perf_counter() - preparation_started) * 1000
                if self.last_policy_metrics is not None:
                    metrics["policy"] = dict(self.last_policy_metrics)
            environment = self._environment(scratch)
            # Worker/control executables retain trusted PATH. Only user commands
            # receive the project environment's command search path.
            if selected and request is None:
                environment['PATH'] = os.pathsep.join([
                    str(aliases), str(selected.executable.parent), environment['PATH']])
                prefix = selected.executable.parent.parent
                if (prefix / 'conda-meta').is_dir():
                    environment['CONDA_PREFIX'] = str(prefix)
                elif (prefix / 'pyvenv.cfg').is_file():
                    environment['VIRTUAL_ENV'] = str(prefix)
            runner = ProcessRunner(max_output_bytes=max_output_bytes,
                                   base_env=environment, supervise_tree=True)
            process_started = perf_counter()
            try:
                result = runner.run(
                    invocation,
                    cwd=cwd or self.workspace, timeout_seconds=timeout,
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
                    "pid": result.pid, "process_group_id": result.process_group_id,
                }
            return result

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        profile = control / "policy.sb"
        profile.write_text(seatbelt_profile(
            self.workspace, scratch, read_paths, self.protected_paths, git_read=git_read,
        ))
        return [str(self.executable), "-f", str(profile), *command]

    def _preflight(self):
        # Verify actual kernel enforcement, rather than trusting executable presence.
        denied = self.directory / "denied.txt"
        denied.write_text("native sandbox probe")
        code = (
            "import errno, os, pathlib, socket, subprocess, sys, tempfile\n"
            "def blocked(action):\n"
            " try: action()\n"
            " except OSError as e:\n"
            "  assert e.errno in (errno.EPERM, errno.EACCES), repr(e)\n"
            " else: raise AssertionError('sandbox restriction missing')\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).read_text())\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).write_text('changed'))\n"
            "blocked(lambda: socket.socket().connect(('127.0.0.1', 9)))\n"
            "p = pathlib.Path(tempfile.gettempdir()) / 'probe'\n"
            "p.write_text('ok'); assert p.read_text() == 'ok'; p.unlink()\n"
            "blocked(lambda: (p.parent / '.ENV').write_text('blocked'))\n"
            f"with tempfile.NamedTemporaryFile(dir={str(self.workspace)!r}, prefix='.native-probe-') as f:\n"
            " f.write(b'probe'); f.flush()\n"
            " blocked(lambda: os.link(f.name, str(p)))\n"
            "subprocess.run([sys.executable, '-I', '-c', "
            f"\"import pathlib; pathlib.Path({str(denied)!r}).read_text()\"], "
            "check=False, capture_output=True).returncode != 0 or sys.exit(1)\n"
            "print('native-ok')\n"
        )
        result = self._run([str(self.python), "-I", "-c", code], timeout=15)
        if result.exit_code != 0 or result.stdout.strip() != "native-ok" or not self.healthy:
            raise ValueError(
                "macOS 原生沙箱自检失败；不会退回未隔离执行。"
                "当前系统或外层沙箱可能不允许 sandbox-exec。"
                + (f"\n{result.stderr[:1500]}" if result.stderr else "")
            )

    def tools(self):
        return [NativeTool(tool.definition, self, execution_kind_of(tool))
                for tool in self._tool_catalog().values()]

    def _tool_catalog(self):
        if not hasattr(self, "_native_tools"):
            self._native_tools = {tool.definition.name: tool for tool in create_default_tools(
                self.workspace, isolated_execution=True, **self._tool_limits(),
            )}
        return self._native_tools

    def _file_tools(self):
        # Only factory-owned built-ins are eligible; a plugin's self-declared
        # attribute or model argument cannot grant host execution privileges.
        if not hasattr(self, "_trusted_file_tools"):
            self._trusted_file_tools = {
                tool.definition.name: tool for tool in create_file_tools(self.workspace)
                if execution_kind_of(tool) is ExecutionKind.TRUSTED_FILE
            }
        return self._trusted_file_tools

    def _execute_file(self, tool, arguments):
        protected = tuple(getattr(self, "protected_paths", ())) + tuple(
            runtime_protected_paths(self.workspace))
        selected = getattr(self, 'project_python', None)
        readonly = (*self.read_paths, *(selected.read_paths if selected else ()))
        access = FileAccess(self.workspace, protected_paths=protected,
                            read_only_paths=readonly)
        try:
            with access.activate():
                result = tool.execute(arguments)
        except OSError:
            result = ToolResult(False, error_code="PERMISSION_DENIED",
                                error="无法安全访问工作区；请检查目录及文件权限")
        return replace(result, data={**result.data, "execution_allowed": self.healthy})

    def _tool_limits(self):
        return {"command_timeout_seconds": self.command_timeout_seconds,
                "python_timeout_seconds": self.python_timeout_seconds}

    def execute(self, workspace, name, arguments):
        started = perf_counter()
        self.last_workspace_check_ms = 0.0
        self._performance_runs = []
        try:
            return self._execute(workspace, name, arguments)
        finally:
            self.last_tool_metrics = {
                "tool": name, "total_ms": (perf_counter() - started) * 1000,
                "workspace_check_ms": self.last_workspace_check_ms,
                "runs": self._performance_runs,
            }
            del self._performance_runs

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
                return ToolResult(False, error_code="NATIVE_UNHEALTHY",
                                  error="进程清理未确认，暂停执行和写入；仍可读取文件和查询环境状态",
                                  data={"execution_allowed": False,
                                        "last_cleanup": getattr(self, "last_cleanup", {})})
        if kind is ExecutionKind.TRUSTED_FILE:
            file_tool = self._file_tools().get(name)
            if file_tool is None:
                raise ValueError(f"Native file tool must be registered by create_file_tools: {name}")
            return self._execute_file(file_tool, arguments)
        self._prepare_workspace()
        if name in {"run_command", "run_python"}:
            # Keep argument/path validation identical to worker tools, but collect
            # program output directly outside the worker's final JSON protocol.
            tool = (
                RunCommandTool(self.workspace, execution_allowed=True,
                               max_timeout_seconds=self.command_timeout_seconds)
                if name == "run_command"
                else RunPythonTool(self.workspace, execution_allowed=True,
                                   python_executable=(self.project_python.executable
                                                      if getattr(self, "project_python", None)
                                                      else self.python),
                                   max_timeout_seconds=self.python_timeout_seconds)
            )
            tool.runner = _NativeCommandRunner(self)
            result = tool.execute(arguments)
            if result.data.get("cleanup_error"):
                return ToolResult(False, error_code="NATIVE_EXECUTION_FAILED",
                                  error="命令已返回，但进程清理未确认；保留已收集的输出",
                                  data={**result.data, "execution_allowed": self.healthy})
            return replace(result, data={**result.data, "execution_allowed": self.healthy})
        request = {
            "name": name, "arguments": arguments,
            "execution_context": self.execution_context(),
            "tool_limits": self._tool_limits(),
        }
        # -I excludes the workspace/PYTHONPATH; trusted package copies take precedence.
        result = self._run(request=request, git_read=name in {"git_status", "git_diff"},
                           project=name not in {"git_status", "git_diff"})
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated or result.cleanup_error:
            return ToolResult(False, error_code="NATIVE_EXECUTION_FAILED",
                              error="原生沙箱执行失败、超时或输出超过限制；文件修改可能已经生效",
                              data=self._failure_data(result))
        try:
            payload = json.loads(result.stdout)
            tool_result = ToolResult(**payload)
        except (ValueError, TypeError):
            return ToolResult(False, error_code="NATIVE_PROTOCOL_ERROR", error="原生 worker 返回无效结果",
                              data=self._failure_data(result))
        if (tool_result.data.get("cleanup_error") or tool_result.data.get("timed_out")
                or tool_result.error_code == "LSP_TIMEOUT"):
            if result.cleanup_status == "confirmed":
                data = dict(tool_result.data)
                data["inner_cleanup_error"] = data.get("cleanup_error")
                data.update(cleanup_error=None, cleanup_status="confirmed",
                            cleanup_diagnostics=result.cleanup_diagnostics)
                tool_result = replace(tool_result, data=data)
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
