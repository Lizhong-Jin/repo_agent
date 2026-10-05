"""Host-side writeback decisions and durable recovery records."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from host_support.storage import atomic_write
from tools.execute import PROCESS_EXECUTION_TOOLS


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(
        path,
        json.dumps(value, ensure_ascii=False).encode("utf-8"),
        prefix=path.name + ".",
        sync=True,
        mode=0o600,
    )


class WritebackGuard:
    """Track exact operations or explicitly named validation retries."""

    def __init__(self):
        self.pending: dict[str, str] = {}
        self.needs_review = False

    def record(self, name, arguments, result):
        # Named checks allow corrected validation code; other operations remain exact.
        normalized = {k: v for k, v in arguments.items() if k != "timeout_seconds"}
        if name in (PROCESS_EXECUTION_TOOLS | {"git_diff", "git_status", "git_log", "git_show"}):
            normalized.setdefault("cwd", ".")
        if name in PROCESS_EXECUTION_TOOLS and arguments.get("check_id"):
            normalized = {"check_id": arguments["check_id"], "cwd": arguments.get("cwd", ".")}
        key = hashlib.sha256(json.dumps([name, normalized], sort_keys=True).encode()).hexdigest()
        data = result.data
        failed = (
            not result.success
            or data.get("timed_out")
            or data.get("cleanup_error")
            or ("exit_code" in data and data["exit_code"] != 0)
            or (name in PROCESS_EXECUTION_TOOLS and data.get("exit_code") != 0)
        )
        if data.get("cleanup_error"):
            self.needs_review = True
        if failed:
            if data.get("cleanup_error"):
                detail = "进程清理未确认"
            elif data.get("timed_out"):
                detail = "执行超时"
            elif not result.success:
                detail = result.error_code or "工具失败"
            else:
                detail = f"退出码 {data.get('exit_code')}"
            label = f" [{arguments['check_id']}]" if arguments.get("check_id") else ""
            self.pending[key] = f"{name}{label}（{detail}）"
        else:
            self.pending.pop(key, None)

    def reason(self, result, backend) -> str | None:
        if not getattr(backend, "healthy", True):
            return "容器清理未确认"
        if result.status != "completed":
            self.needs_review = True
            return "任务未正常完成，请检查副本后手动 /apply"
        if self.needs_review:
            return "有中断、未完成任务或恢复操作留下的改动，请检查后手动 /apply"
        if self.pending:
            return "仍有未解决的工具或命令失败：" + ", ".join(sorted(set(self.pending.values())))
        if result.stats is None:
            self.needs_review = True
            return "缺少任务执行记录"
        if any(call.error_code == "UNKNOWN_TOOL" for call in result.stats.tool_calls):
            self.needs_review = True
            return "任务包含未知工具调用"
        return None


class Backup:
    def __init__(self, session, host, current, changed):
        from .session import fingerprint

        self.directory = session.directory / "backups" / uuid4().hex
        self.directory.mkdir(parents=True, mode=0o700)
        self.state = {"root": str(session.root), "status": "prepared", "files": {}}
        for index, name in enumerate(changed):
            before = host.get(name)
            after = current.get(name)
            blob = str(index)
            if before is not None:
                with (self.directory / blob).open("xb") as output:
                    os.chmod(self.directory / blob, 0o600)
                    output.write(before[0])
                    output.flush()
                    os.fsync(output.fileno())
            self.state["files"][name] = {
                "blob": blob if before is not None else None,
                "before": fingerprint(*before) if before is not None else None,
                "mode": before[1] if before is not None else None,
                "after": fingerprint(*after) if after is not None else None,
                "attempted": False,
            }
        self.save()

    def save(self):
        atomic_json(self.directory / "manifest.json", self.state)

    def attempting(self, name):
        self.state["status"] = "applying"
        self.state["files"][name]["attempted"] = True
        self.save()  # Persist intent before changing the original file.

    def complete(self):
        self.state["status"] = "complete"
        self.save()
