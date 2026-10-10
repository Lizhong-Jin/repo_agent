"""Windows native: LPAC, private runtime/workspace, confirmed-cleanup publication."""

import json
import ntpath
import os
import re
import socket
import sys
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from host_support.cancellation import defer_cancellation
from host_support.paths import app_directory, find_windows_executable
from host_support.windows_isolation import WindowsIsolationAPI
from host_support.windows_recovery import recover_profiles
from host_support.windows_security import PrivateWindowsSecurity
from tools.git_tools import GitDiffTool, GitLogTool, GitShowTool, GitStatusTool

from .native_common import NativeBackendBase
from .native_execution import NativeCleanupError
from .windows_execution import WindowsLaunch, WindowsNativeExecutionAdapter
from .windows_git import WindowsGitRunner, copy_git_runtime, validate_git_query
from .windows_python import inspect_python, select_project, stage_python
from .windows_workspace import WindowsCallSnapshot, copy_private_tree


class WindowsNativeBackend(NativeBackendBase):
    execution_adapter_type = WindowsNativeExecutionAdapter
    platform_name = "windows"
    isolation = "lpac+job-object"

    def _platform_setup(self):
        if sys.platform != "win32":
            raise ValueError("Windows native requires Windows x86_64")
        self.windows_api = WindowsIsolationAPI()
        self.security = PrivateWindowsSecurity(self.windows_api)
        self.windows_state_root = app_directory("state") / "windows-native"
        self.windows_state_root.mkdir(parents=True, exist_ok=True)
        self.security.set_acl(self.windows_state_root)
        self.recovery_report = recover_profiles(self.windows_state_root, self.windows_api)
        if self.recovery_report["failed"]:
            raise ValueError(f"Windows native 残留回收失败：{self.recovery_report['failed']}")
        self.system_directory = Path(self.windows_api.windows_directory()) / "System32"
        self._checking_isolation = False
        self.last_writeback = None

    def _read_paths(self):
        self.security.set_acl(self.directory)
        self._agent_layout = inspect_python(
            self.python, "agent fallback", self.workspace, trusted_base=sys.base_prefix
        )
        self._project_layout = self._agent_layout
        return (self.runtime, *self._agent_layout.read_paths)

    def _prepare_workspace(self):
        # Descriptor snapshot checks run again for each process call. File tools
        # retain their own handle-relative policy, without an extra global scan.
        self._set_metric("last_workspace_check_ms", 0.0)
        self.protected_paths = tuple(
            dict.fromkeys((*self.protected_paths, self.windows_state_root))
        )

    def _select_windows_project_python(self, explicit):
        selected, self._project_layout = select_project(
            self.workspace, explicit, self.python, isolated_workspace=self.isolated_workspace
        )
        return selected

    def execution_context(self):
        context = super().execution_context()
        context.update(
            writeback_mode="per_call_after_cleanup",
            process_workspace="filtered_private_copy",
            command_path_policy="private_python_system32_or_workspace_exe",
            git_execution_policy="read_only_direct_git_lpac_no_children",
            last_writeback=self.last_writeback,
            recovery=self.recovery_report,
        )
        context["persistence"]["tmp_across_calls"] = False
        return context

    def _execute(self, workspace, name, arguments):
        queries = {
            "git_status": GitStatusTool,
            "git_diff": GitDiffTool,
            "git_log": GitLogTool,
            "git_show": GitShowTool,
        }
        if name not in queries or not self.healthy:
            return super()._execute(workspace, name, arguments)
        if Path(workspace).resolve() != self.workspace:
            raise ValueError("Native 后端不能切换工作区")
        self._prepare_workspace()
        # Validation/parsing stays in trusted built-ins. Only their generated Git
        # commands enter the compatibility profile; no Python worker runs there.
        tool = queries[name](self.workspace, execution_allowed=True)
        tool.runner = WindowsGitRunner(self, tool.max_output_bytes)
        result = tool.execute(arguments)
        return replace(result, data={**result.data, "execution_allowed": self.healthy})

    def _preflight(self):
        secret = self.directory / "host-only.txt"
        secret.write_text("native-preflight-private")
        self._checking_isolation = True
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                # Paths for the copied runtime/workspace are taken from a sealed
                # environment, not calculated from the original host installation.
                code = """
import json, os, pathlib, socket
checks = {}
def denied(name, operation):
    try: operation()
    except PermissionError: checks[name] = True
    else: checks[name] = False
denied('host_read', lambda: pathlib.Path(HOST_SECRET).read_bytes())
denied('host_write', lambda: pathlib.Path(HOST_SECRET).write_text('modified'))
runtime = pathlib.Path(os.environ['AGENT_PRIVATE_RUNTIME'])
denied('runtime_write', lambda: (runtime / 'bad.py').write_text('x'))
def network_access():
    # Windows may reject socket creation before connect is reached.
    with socket.socket() as sock:
        sock.settimeout(2)
        sock.connect(('127.0.0.1', PORT))
denied('network', network_access)
pathlib.Path('native-write-test.txt').write_text('allowed')
checks['workspace_write'] = pathlib.Path('native-write-test.txt').read_text() == 'allowed'
print(json.dumps(checks))
"""
                code = f"HOST_SECRET = {str(secret)!r}\nPORT = {listener.getsockname()[1]}\n" + code
                result = self._run([str(self.python), "-I", "-c", code], timeout=30)
                expected = {
                    "host_read",
                    "host_write",
                    "runtime_write",
                    "network",
                    "workspace_write",
                }
                report = None
                if result.exit_code == 0:
                    try:
                        report = json.loads(result.stdout)
                    except ValueError:
                        pass  # Include malformed output in the same bounded diagnostic below.
                if (
                    not isinstance(report, dict)
                    or set(report) != expected
                    or any(value is not True for value in report.values())
                    or result.timed_out
                    or result.stdout_truncated
                    or result.stderr_truncated
                    or result.cleanup_status != "confirmed"
                    or not self.healthy
                ):
                    raise ValueError(
                        "Windows native 隔离自检失败；不会退回未隔离执行："
                        + self._preflight_diagnostic(result, report)
                    )
                self.preflight_metrics = {"isolation_verified": True, "checks": report}
        finally:
            self._checking_isolation = False
            secret.unlink(missing_ok=True)

    def _preflight_diagnostic(self, result, report):
        code = result.exit_code
        return json.dumps(
            {
                "exit_code": code,
                "exit_code_hex": f"0x{code & 0xFFFFFFFF:08X}" if code is not None else None,
                "timed_out": result.timed_out,
                "cleanup_status": result.cleanup_status,
                "cleanup_error": (result.cleanup_error or "")[:1000],
                "healthy": self.healthy,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
                "checks": repr(report)[:1000],
                "stdout": result.stdout[:1000],
                "stderr": result.stderr[:1000],
            },
            ensure_ascii=False,
        )

    @contextmanager
    def _prepare_windows_call(self, isolation, call):
        profile = isolation.profile.directory
        sid = self.security.sid_string(isolation.profile.sid)
        runtime = profile / "runtime"
        scratch = profile / "scratch"
        scratch.mkdir()
        git_query = call.git_read
        if git_query and (call.request is not None or not call.command or call.command[0] != "git"):
            raise ValueError("Windows Git compatibility requires a trusted direct query")
        if git_query:
            validate_git_query(call.command)
        chosen = self._project_layout if call.project else self._agent_layout
        python_paths = ()
        mapping = []
        if not git_query:
            copy_private_tree(self.runtime, runtime)
            agent, agent_base = stage_python(self._agent_layout, profile / "agent-python")
            if chosen == self._agent_layout:
                project, project_base = agent, agent_base
            else:
                project, project_base = stage_python(chosen, profile / "project-python")
            python_paths = (project.parent, project_base)
            mapping = [
                (
                    chosen.root,
                    project.parent.parent
                    if chosen.executable.parent.name.lower() == "scripts"
                    else project.parent,
                ),
                (
                    self._agent_layout.root,
                    agent.parent.parent
                    if self._agent_layout.executable.parent.name.lower() == "scripts"
                    else agent.parent,
                ),
            ]
        control = self.windows_state_root / "calls" / isolation.lease.identity
        snapshot = WindowsCallSnapshot(
            self.workspace,
            profile / "workspace",
            control,
            protected_paths=(
                *self.protected_paths,
                self.windows_state_root,
                *self._agent_layout.read_paths,
                *chosen.read_paths,
            ),
        )
        completed = False
        try:
            git_paths = self._stage_git(profile, snapshot.workspace, call) if git_query else ()
            environment = {
                "SystemRoot": str(self.system_directory.parent),
                "WINDIR": str(self.system_directory.parent),
                "PATH": os.pathsep.join(
                    map(str, (*python_paths, *git_paths, self.system_directory))
                ),
                "TEMP": str(scratch),
                "TMP": str(scratch),
                "HOME": str(scratch),
                "USERPROFILE": str(scratch),
                "APPDATA": str(scratch),
                "LOCALAPPDATA": str(scratch),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "AGENT_PRIVATE_RUNTIME": str(runtime),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "NUL",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_CONFIG_COUNT": "4",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": "*",
                "GIT_CONFIG_KEY_1": "core.longpaths",
                "GIT_CONFIG_VALUE_1": "true",
                "GIT_CONFIG_KEY_2": "protocol.allow",
                "GIT_CONFIG_VALUE_2": "never",
                "GIT_CONFIG_KEY_3": "core.hooksPath",
                "GIT_CONFIG_VALUE_3": "NUL",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LITERAL_PATHSPECS": "1",
            }
            mapping.append((self.workspace, snapshot.workspace))
            cwd = snapshot.workspace / Path(call.cwd).relative_to(self.workspace)
            if git_query:
                command = (str(git_paths[0] / "git.exe"), *call.command[1:])
            elif call.request is not None:
                request = deepcopy(call.request)
                context = request.get("execution_context", {})
                context.setdefault("python_environments", {})["project"] = str(project)
                request["execution_context"] = context
                request_path = profile / "request.json"
                request_path.write_text(json.dumps(request), encoding="utf-8")
                bootstrap = (
                    f"import sys,json; sys.path.insert(0,{str(runtime)!r}); "
                    "from sandbox.worker import execute_request; "
                    f"execute_request(json.load(open({str(request_path)!r},encoding='utf-8')),"
                    f"{str(snapshot.workspace)!r})"
                )
                command = (str(agent), "-I", "-c", bootstrap)
            else:
                command = self._command(
                    call.command, cwd, mapping, project, agent, (*git_paths, self.system_directory)
                )
            # The profile root itself must not permit deleting/replacing read-only
            # runtime children. Only workspace and scratch receive writable ACLs.
            self.security.seal_tree(profile, sid)
            self.security.seal_tree(snapshot.workspace, sid, writable=not git_query)
            for metadata in snapshot.workspace.rglob(".git"):
                self.security.seal_tree(metadata, sid)
            self.security.seal_tree(scratch, sid, writable=True)
            if not self._checking_isolation:
                isolation.retain(
                    "执行中或未完成回写；项目副本："
                    + str(snapshot.workspace)
                    + "；恢复记录："
                    + str(control)
                )
            yield WindowsLaunch(command, cwd, environment, git_query, snapshot.workspace)
            execution = isolation.execution
            if (
                execution is None
                or execution.result is None
                or execution.last_cleanup_status != "confirmed"
            ):
                raise NativeCleanupError("Windows 调用未确认完成；保留项目副本：" + str(profile))
            if not self._checking_isolation:
                with defer_cancellation():
                    try:
                        changed = [] if git_query else snapshot.publish()
                    except BaseException as error:
                        isolation.retain(
                            "回写未完成；项目副本："
                            + str(snapshot.workspace)
                            + "；恢复记录："
                            + str(control)
                        )
                        raise NativeCleanupError(
                            "Windows 回写失败，副本和备份已保留：" + str(control)
                        ) from error
                    self.last_writeback = {"status": "applied", "files": changed}
                    isolation.lease.record["retain"] = False
                    isolation.lease.save()
                    isolation.retention_reason = None
            completed = True
        finally:
            never_started = isolation.execution is None or not isolation.execution.started
            if completed or never_started:
                if never_started and isolation.lease:
                    isolation.lease.record["retain"] = False
                    isolation.lease.save()
                    isolation.retention_reason = None
                snapshot.discard_control()

    def _command(self, command, cwd, mapping, project, agent, search):
        def mapped(value):
            path = Path(value)
            if path.is_absolute():
                for original, private in mapping:
                    if path.is_relative_to(original):
                        return str(private / path.relative_to(original))
            return value

        values = [mapped(value) for value in command]
        if command[0] == str(self.python):
            values[0] = str(agent)
        elif ntpath.basename(command[0]).lower() in {
            "python",
            "python3",
            "python.exe",
            "python3.exe",
        } and not ntpath.dirname(command[0]):
            values[0] = str(project)
        elif not Path(values[0]).is_absolute():
            name = values[0] if values[0].lower().endswith(".exe") else values[0] + ".exe"
            candidates = [cwd / name] if ntpath.dirname(name) else [path / name for path in search]
            executable = next((path for path in candidates if path.is_file()), None)
            if executable is None:
                raise ValueError("Windows native 可执行文件不在已授权工具路径内：" + command[0])
            values[0] = str(executable)
        allowed = (self.system_directory, *(private for _, private in mapping), *search)
        if not any(Path(values[0]).is_relative_to(path) for path in allowed):
            raise ValueError("Windows native 拒绝运行未授权的宿主可执行文件")
        return tuple(values)

    def _stage_git(self, profile, workspace, call):
        if self._checking_isolation or not call.git_read:
            return ()
        git = find_windows_executable("git", exclude=(self.workspace, self.directory))
        if not git:
            if call.git_read:
                raise ValueError("Windows native Git 工具需要 Git for Windows")
            return ()
        executable = Path(git)
        root = executable.parent.parent
        if (
            executable.parent.name.lower() not in {"cmd", "bin"}
            or not (root / "mingw64/bin/git.exe").is_file()
        ):
            if call.git_read:
                raise ValueError("Windows native requires the standard Git for Windows layout")
            return ()
        target = profile / "git-runtime"
        copy_git_runtime(root / "mingw64/bin", target / "bin")
        if call.git_read:
            cwd = Path(call.cwd).resolve(strict=True)
            if not cwd.is_relative_to(self.workspace):
                raise ValueError("Git cwd is outside the workspace")
            while True:
                metadata = cwd / ".git"
                if metadata.exists():
                    if not metadata.is_dir() or metadata.is_symlink():
                        raise ValueError(
                            "Linked Git metadata requires the managed worktree interface"
                        )
                    destination = workspace / cwd.relative_to(self.workspace) / ".git"
                    copy_private_tree(
                        metadata,
                        destination,
                        allowed=lambda name: (
                            name.split("/")[0]
                            in {"HEAD", "index", "packed-refs", "refs", "objects", "shallow"}
                            and name != "objects/info/alternates"
                        ),
                    )
                    # Do not load repository hooks, filter executables or external gitdir pointers.
                    config = "[core]\nrepositoryformatversion = 0\n"
                    if (metadata / "config").is_file():
                        from host_support.filesystem import open_file

                        with os.fdopen(
                            open_file(metadata / "config"), "r", encoding="utf-8"
                        ) as source:
                            if re.search(
                                r"(?im)^\s*objectformat\s*=\s*sha256\s*$", source.read(65536)
                            ):
                                config = (
                                    "[core]\nrepositoryformatversion = 1\n"
                                    "[extensions]\nobjectFormat = sha256\n"
                                )
                    (destination / "config").write_text(config, encoding="utf-8")
                    break
                if cwd == self.workspace:
                    break
                cwd = cwd.parent
        return (target / "bin",)
