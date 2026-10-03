"""Docker boundary. No fallback to host execution."""

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from host_support.cancellation import RunCancelled, current_cancellation
from tools._internal.base import ToolResult
from tools._internal.process_runner import ProcessRunner

from .backend import SandboxBackend as SandboxBackend
from .concurrency import backend_gate
from .policy import SandboxPolicy


class DockerBackend:
    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        self.healthy = True
        self.executable = shutil.which("docker")
        if not self.executable:
            raise ValueError("Docker 不可用；不会退回宿主机执行。请安装并启动 Docker。")
        check = subprocess.run(
            [self.executable, "image", "inspect", policy.image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if check.returncode:
            raise ValueError(f"Docker 镜像不可用；请先构建 {policy.image}。")
        self.image = check.stdout.strip()  # Pin this session to the inspected image ID.
        self.runner = ProcessRunner(max_output_bytes=4 * 1024 * 1024)

    def execution_context(self) -> dict:
        """Only report policy actually used by this backend; never host totals."""
        return {
            "mode": "docker",
            "image_id": self.image,
            "network": "disabled",
            "writable_paths": ["/workspace", "/tmp", "/dev/shm"],
            "root_filesystem": "read_only",
            "changes_apply_to": "workspace_copy",
            "resources": {
                "cpu_limit": self.policy.cpus,
                "memory_limit": self.policy.memory,
                "pids_limit": self.policy.pids,
                "tmpfs_size": self.policy.tmpfs_size,
                "shm_size": self.policy.shm_size,
                "max_file_bytes": self.policy.max_file_bytes,
                "max_workspace_bytes": self.policy.max_workspace_bytes,
                "container_timeout_seconds": self.policy.timeout,
                "gpu_selection": self.policy.gpus,
            },
            "persistence": {
                "workspace_files_across_calls": True,
                "processes_across_calls": False,
                "tmp_across_calls": False,
            },
        }

    def execute(self, workspace: Path, name: str, arguments: dict) -> ToolResult:
        with backend_gate(self).hold():
            return self._execute(workspace, name, arguments)

    def _execute(self, workspace: Path, name: str, arguments: dict) -> ToolResult:
        if not self.healthy:
            raise OSError("之前的容器清理未确认，拒绝继续执行。")
        container = "repo-agent-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="agent-request-") as directory:
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "name": name,
                        "arguments": arguments,
                        "execution_context": self.execution_context(),
                        "tool_limits": {
                            "command_timeout_seconds": self.policy.command_timeout_seconds,
                            "python_timeout_seconds": self.policy.python_timeout_seconds,
                        },
                    }
                )
            )
            request.chmod(0o644)
            command = [
                self.executable,
                "run",
                "--rm",
                "--pull=never",
                "--name",
                container,
                "--network=none",
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--memory",
                self.policy.memory,
                "--memory-swap",
                self.policy.memory,
                "--cpus",
                self.policy.cpus,
                "--pids-limit",
                str(self.policy.pids),
                "--ulimit",
                f"fsize={self.policy.max_file_bytes}:{self.policy.max_file_bytes}",
                "--tmpfs",
                f"/tmp:rw,nosuid,nodev,size={self.policy.tmpfs_size},mode=1777",
                "--shm-size",
                self.policy.shm_size,
                "--env",
                "HOME=/tmp",
                "--env",
                "TMPDIR=/tmp",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace",
                "--mount",
                f"type=bind,src={request},dst=/request.json,readonly",
                "--workdir",
                "/workspace",
                *(
                    ["--gpus", "all" if self.policy.gpus == "all" else f"device={self.policy.gpus}"]
                    if self.policy.gpus is not None
                    else []
                ),
                self.image,
            ]
            try:
                result = self.runner.run(
                    command, cwd=workspace, timeout_seconds=self.policy.timeout
                )
            finally:
                # Killing the Docker client alone does not terminate its container.
                context = current_cancellation()
                try:
                    cleanup = subprocess.run(
                        [self.executable, "rm", "-f", container],
                        capture_output=True,
                        timeout=15,
                        check=False,
                    )
                    if cleanup.returncode and b"No such container" not in cleanup.stderr:
                        raise OSError("Sandbox 容器清理失败；停止会话，请检查 Docker。")
                except BaseException as error:
                    self.healthy = False
                    if context is not None:
                        context.record_cleanup(
                            "unknown", container=container, error=type(error).__name__
                        )
                        if context.event.is_set():
                            raise RunCancelled(context) from error
                    raise
                if context is not None:
                    context.record_cleanup("confirmed", container=container)
            if result.timed_out or result.exit_code != 0 or result.stdout_truncated:
                return ToolResult(
                    False,
                    error_code="SANDBOX_EXECUTION_FAILED",
                    error="Sandbox 执行失败、超时或结果超过限制。",
                )
            try:
                payload = json.loads(result.stdout)
                return ToolResult(**payload)
            except (ValueError, TypeError):
                return ToolResult(
                    False, error_code="SANDBOX_PROTOCOL_ERROR", error="Sandbox 返回了无效结果。"
                )
