"""Read-only recovery diagnosis and revision-bound, explicitly selected actions.

No model execution, tool replay, automatic queue resume, or historical success repair.
The caller owns the project execution lock for standalone commands.
"""

import os
import subprocess
from copy import copy, deepcopy

from host_support.git_worktree import Git
from host_support.storage import sync_directory
from llm import Message
from tools._internal.operation_journal import OperationJournal, fingerprint

from . import delivery
from .execution_ledger import ExecutionLedger
from .session import SESSION_ID, _read, validate_record
from .task_queue import TaskQueue
from .task_reports import fingerprint as content_fingerprint
from .task_reports import snapshot
from .workspaces import Workspace, save_json


def optional(path):
    if path.parent.resolve() != path.parent.absolute():
        raise ValueError("恢复记录路径不能经过符号链接")
    return _read(path) if os.path.lexists(path) else None


def select_snapshot(store, selector):
    """Unlike select()/catalog.resolve(), never repairs or registers a session."""
    if store.directory.resolve() != store.directory.absolute():
        raise ValueError("会话目录不能经过符号链接")
    index = store.catalog.inspect_index()
    pending = optional(store.directory / ".pending-save")
    latest = optional(store.directory / "latest.json")
    if selector == "latest":
        sid = latest.get("session_id") if latest else pending.get("session_id") if pending else None
    elif SESSION_ID.fullmatch(selector):
        sid = selector
    else:
        matches = [
            sid for sid, row in index["sessions"].items() if selector == str(row["sequence"])
        ]
        if not matches:
            matches = [sid for sid, row in index["sessions"].items() if selector == row["name"]]
        if len(matches) != 1:
            raise ValueError("找不到唯一会话；请指定完整会话 ID")
        sid = matches[0]
    if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
        raise ValueError("没有可检查的会话，请指定 --session 完整ID")
    data = optional(store.directory / (sid + ".json"))
    if data is not None:
        validate_record(data, store.project, sid)
    if pending is not None:
        validate_record(pending, store.project, pending.get("session_id"))
    view = copy(store)
    view.id, view.data = sid, data
    return view, {"index": index, "pending": pending, "latest": latest, "session": data}


class Recovery:
    def __init__(self, store, selector="latest", *, controller=None):
        self.store, self.selector, self.controller = store, selector, controller

    def inspect(self):
        issues, actions = [], []

        def issue(code, message, blocks=(), action=None):
            issues.append({"code": code, "message": message, "blocks": list(blocks)})
            if action and action not in actions:
                actions.append(action)

        # Do not wait on a permission/service lock held by an in-flight HTTP call.
        # Active diagnosis cannot offer a stable apply plan anyway.
        if self.controller and (self.controller.active or self.controller.closing):
            return {
                "session_id": self.store.id,
                "plan": None,
                "actions": [],
                "operations": [],
                "live": True,
                "issues": [
                    {
                        "code": "execution.active",
                        "message": "任务或收尾仍在运行；等待结束后生成恢复计划",
                        "blocks": ["apply"],
                    }
                ],
            }
        try:
            view, evidence = select_snapshot(self.store, self.selector)
            data = deepcopy(view.data)
            live = self.controller
            if live:
                # Bind plans to the unsaved in-memory state as well as disk evidence.
                evidence["live"] = {
                    "record": live.conversation._record(),
                    "active": bool(live.active),
                    "closing": live.closing,
                    "persistence_blocked": live.persistence_blocked,
                    "cleanup_blocked": live.cleanup_blocked,
                    "healthy": live._healthy(),
                }
                data = evidence["live"]["record"]
                if live.persistence_blocked or live.conversation.save_error:
                    issue(
                        "storage.unsaved",
                        "内存状态尚未保存；可重试保存当前状态",
                        ["execute"],
                        "checkpoint",
                    )
                permissions = getattr(live.runtime.access_policy, "permissions", None)
                if permissions and permissions.pending:
                    issue(
                        "permission.pending",
                        "使用 /permissions approve ID 或 deny ID，再 /continue",
                        ["continue"],
                    )
            if evidence["pending"]:
                issue(
                    "storage.pending_commit",
                    "存在未完成的会话提交；先补完保存，再重新生成计划",
                    ["apply"],
                    "repair-save",
                )
            workspace = Workspace(view)
            if (
                data
                and data.get("workspace_id")
                and (not workspace.data or data["workspace_id"] != workspace.id)
            ):
                raise ValueError("会话绑定的工作区记录缺失或不匹配；不能回退到原项目")
            if workspace.data and workspace.data["state"] in {"ready", "running", "blocked"}:
                workspace.validate()
            evidence["workspace"] = workspace.data
            root = workspace.root if workspace.data else view.project
            if data is None:
                issue(
                    "session.missing",
                    "会话快照缺失；保留工作区，不能补造执行成功记录",
                    ["execute", "delivery"],
                )
            if workspace.data:
                state = workspace.data["state"]
                if state in {"creating", "merging"}:
                    issue(
                        "workspace." + state,
                        "工作区操作中断；核实 Git 状态后恢复",
                        ["execute", "merge"],
                        "workspace-recover",
                    )
                elif state in {"running", "blocked"}:
                    issue(
                        "workspace.blocked",
                        "须人工核实本次执行及全部遗留进程已停止",
                        ["execute", "merge"],
                        "confirm-cleanup",
                    )
                elif state in {"archived", "merged"}:
                    issue("workspace." + state, "工作区已归档或合并；新任务请新建会话", ["execute"])
                if workspace.root.exists():
                    evidence["git"] = {
                        "source": Git(view.project).head(),
                        "source_branch": Git(view.project).branch(),
                        "workspace": Git(root).head(),
                    }
            snap = (
                content_fingerprint(snapshot(root, exclude=(view.directory,), texts=False))
                if root.exists()
                else {"hash": "absent", "complete": True}
            )
            snap.pop("at", None)
            evidence["content"] = snap
            if not snap["complete"]:
                issue("content.incomplete", "文件观察不完整；不能绑定修改状态的恢复计划", ["apply"])
            if data:
                queue = TaskQueue(data.get("task_queue"))
                ledger_view = copy(view)
                ledger_view.data = data
                ledger = ExecutionLedger(ledger_view)
                watermark = ledger.watermark()
                evidence["ledger_watermark"] = watermark
                cursor = data.get("ledger_cursor", 0)
                if watermark < cursor:
                    issue(
                        "ledger.behind",
                        "账本早于快照；须恢复匹配备份，不能继续执行",
                        ["execute", "apply"],
                    )
                elif watermark > cursor:
                    issue(
                        "ledger.ahead",
                        "快照之后存在执行证据；恢复时保留证据并暂停队列",
                        ["execute"],
                        "reconcile-session",
                    )
                if queue.running or data.get("pending_task") is not None:
                    issue(
                        "task.interrupted",
                        "执行未确认完成；标记中断，不重放工具",
                        ["execute"],
                        "reconcile-session",
                    )
                if queue.paused:
                    issue("queue.paused", "队列保持暂停；核实后显式 /queue resume")
                executed = [task for task in queue.data["tasks"] if task["attempts"]]
                previous = max(
                    executed, key=lambda task: task["attempts"][-1]["started_at"], default=None
                )
                if previous and previous["state"] in {
                    "failed",
                    "cancelled",
                    "interrupted",
                    "needs_input",
                }:
                    issue(
                        "task.last_" + previous["state"],
                        f"任务 #{previous['number']} 未正常完成；/report 核实后选择续接或新尝试",
                    )
                if not data.get("sandbox_healthy", True) or live and live.cleanup_blocked:
                    issue(
                        "execution.cleanup_unknown",
                        "进程清理未确认；核实后显式确认，历史结果仍不改写",
                        ["execute", "delivery"],
                        "confirm-cleanup",
                    )
                policy = delivery.validate_policy(data.get("delivery_policy", delivery.DEFAULT))
                previous = delivery.latest_task(queue.data)
                if policy["mode"] == "strict" and previous:
                    # Do not call Workspace.snapshot(): it writes Git objects.
                    issue(
                        "delivery.check_required",
                        "严格交付须重判；不会执行检查或接受例外",
                        ["delivery"],
                        "refresh-delivery",
                    )
                    directory = view.directory / "reports" / view.id
                    evidence["reports"] = {
                        p.name: fingerprint(optional(p)) for p in sorted(directory.glob("*.json"))
                    }
            journal = OperationJournal(view.directory, view.id)
            records = journal.records()
            evidence["operations"] = records
            receipts = {
                p.name: optional(p) for p in sorted((view.directory / "recovery").glob("*.json"))
            }
            evidence["recovery_receipts"] = receipts
            for name, receipt in receipts.items():
                if receipt.get("state") == "started":
                    issue(
                        "recovery.interrupted",
                        f"恢复动作收尾未确认：{name}；按当前状态重新制定计划",
                    )
            operations = []
            for record in records:
                checked = record.get("verification", {}).get("state")
                operations.append(
                    {
                        "id": record["id"],
                        "repo": record["scope"]["repo"],
                        "operation": record["scope"]["operation"],
                        "stage": record["stage"],
                        "verification": checked,
                        "verification_detail": record.get("verification"),
                        "record_digest": fingerprint(record),
                    }
                )
                if record["stage"] != "completed" and checked != "confirmed":
                    issue(
                        "external.unconfirmed",
                        f"外部操作 {record['id']} 需要核实；禁止自动重试",
                        ["retry_external"],
                    )
                actions.append("verify:" + record["id"])
            if evidence["pending"]:
                actions = ["checkpoint"] if live and "checkpoint" in actions else ["repair-save"]
            if any(
                "apply" in row["blocks"] and row["code"] != "storage.pending_commit"
                for row in issues
            ):
                actions = []
            if any(
                row["code"]
                in {
                    "ledger.ahead",
                    "task.interrupted",
                    "execution.cleanup_unknown",
                    "workspace.blocked",
                    "workspace.creating",
                    "workspace.merging",
                }
                for row in issues
            ):
                actions = [action for action in actions if action != "refresh-delivery"]
            if any(row["code"] == "task.interrupted" for row in issues):
                actions = [action for action in actions if action != "confirm-cleanup"]
            if live:
                actions = [action for action in actions if action == "checkpoint"]
            return {
                "session_id": view.id,
                "plan": fingerprint(evidence),
                "issues": issues,
                "actions": actions,
                "operations": operations,
                "live": live is not None,
            }
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ) as error:
            # A diagnostic failure must never silently yield an empty/healthy plan.
            return {
                "session_id": self.selector,
                "plan": None,
                "actions": [],
                "operations": [],
                "issues": [
                    {
                        "code": "evidence.unreadable",
                        "message": (
                            f"证据无法完整读取（{type(error).__name__}）；保留原记录并人工核实"
                        ),
                        "blocks": ["apply", "execute"],
                    }
                ],
            }

    def apply(self, token, action, *, confirm_stopped=False, note=None, github=None):
        if self.store._lock_fd is None:
            raise ValueError("恢复操作必须持有项目执行锁")
        report = self.inspect()
        if not token or report["plan"] != token or action not in report["actions"]:
            raise ValueError("恢复计划已过期、动作不适用或证据不可读；请重新运行 recover plan")
        if action == "confirm-cleanup" and (not confirm_stopped or not note or not note.strip()):
            raise ValueError("确认清理需要 --confirm-stopped 和 --note，说明核实的进程及范围")
        if note is not None and (len(note) > 2000 or not note.strip()):
            raise ValueError("核实说明须为 1–2000 个字符")
        if action.startswith("verify:") and github is None:
            raise ValueError("远端核实需要 --allow-network；仅查询这项操作，不恢复写入授权")
        receipt = self.store.directory / "recovery" / (token + ".json")
        if not receipt.parent.exists():
            receipt.parent.mkdir(mode=0o700)
            sync_directory(self.store.directory)
        # A prior start is evidence of interruption, not permission to replay a write.
        save_json(receipt, {"plan": token, "action": action, "state": "started", "note": note})
        if action == "checkpoint":
            self.controller.queue.pause("恢复保存完成；核实后显式恢复队列")
            self.controller._save()
        elif action == "repair-save":
            with self.store.catalog.locked():
                pass
        else:
            view, _ = select_snapshot(self.store, report["session_id"])
            data = deepcopy(view.data)
            workspace = Workspace(view)
            if action.startswith("verify:"):
                operation_id = action.split(":", 1)[1]
                journal = OperationJournal(view.directory, view.id)
                journal.verify(
                    operation_id,
                    github,
                    expected=next(
                        row["record_digest"]
                        for row in report["operations"]
                        if row["id"] == operation_id
                    ),
                )
            elif action == "workspace-recover":
                workspace.recover()
            elif action == "confirm-cleanup":
                if workspace.data and workspace.data["state"] in {"running", "blocked"}:
                    workspace.recover(confirm_stopped=True)
                if data:
                    data["sandbox_healthy"] = True
                    data["recovery_cleanup"] = {
                        "plan": token,
                        "note": note.strip(),
                        "basis": "user_confirmation",
                    }
            elif action == "reconcile-session":
                ledger = ExecutionLedger(view)
                queue = TaskQueue(data.get("task_queue"))
                if (
                    queue.running
                    or data.get("pending_task") is not None
                    or ledger.unconfirmed(after=data.get("ledger_cursor", 0))
                ):
                    # A crashed owner cannot attest that its descendants stopped.
                    data["sandbox_healthy"] = False
                if data.get("pending_task") and "task_queue" not in data:
                    legacy = queue.add(data["pending_task"])
                    legacy["state"] = "interrupted"
                    legacy["result"] = {"notice": "旧版任务未确认，不自动重放"}
                queue.recover()
                data["task_queue"] = queue.snapshot()
                # Persist explanatory history before advancing the evidence watermark.
                data.setdefault("history", []).append(
                    Message(
                        "user",
                        "[恢复核实记录；不是新任务] 执行曾中断，不得自动重放。\n"
                        + (data.get("pending_task") or "")
                        + "\n"
                        + ledger.describe(after=data.get("ledger_cursor", 0)),
                    ).to_dict()
                )
                data["pending_task"] = None
                data["ledger_cursor"] = ledger.watermark()
            elif action == "refresh-delivery":
                if (
                    not data.get("sandbox_healthy")
                    or workspace.data
                    and workspace.data["state"] != "ready"
                ):
                    raise ValueError("先恢复执行环境和工作区")
                queue = TaskQueue(data.get("task_queue"))
                if queue.running or data.get("pending_task") is not None:
                    raise ValueError("先核实中断任务")
                previous = delivery.latest_task(queue.data)
                previous["result"]["delivery"] = delivery.publish(
                    view,
                    previous,
                    data["delivery_policy"],
                    workspace.root if workspace.data else view.project,
                    workspace if workspace.data else None,
                )
                data["task_queue"] = queue.snapshot()
            if data and not action.startswith("verify:"):
                queue = TaskQueue(data.get("task_queue"))
                queue.pause("恢复操作完成；核实后显式恢复队列")
                data["task_queue"] = queue.snapshot()
                view.save(data)
        result = self.inspect()
        save_json(
            receipt,
            {
                "plan": token,
                "action": action,
                "state": "completed",
                "note": note,
                "result_plan": result["plan"],
            },
        )
        return self.inspect()


def describe(report):
    lines = [f"恢复检查：{report['session_id']}"]
    for row in report["issues"]:
        lines.append(f"[{row['code']}] {row['message']}")
    if not report["issues"]:
        lines.append("未发现已知恢复阻塞；这不表示任务或交付已通过。")
    for operation in report["operations"]:
        lines.append(
            f"操作 {operation['id']}：{operation['repo']} / {operation['operation']}；"
            f"发送阶段 {operation['stage']}；核实 {operation['verification'] or '未核实'}"
        )
        if operation.get("verification_detail"):
            detail = operation["verification_detail"]
            lines.append("  " + detail.get("reason", ""))
            if detail.get("candidates"):
                lines.append("  待人工核实 PR：" + ", ".join(map(str, detail["candidates"])))
    if report["plan"]:
        lines.append("计划：" + report["plan"])
    if report["actions"]:
        lines.append("可选动作（每次只应用一项）：" + ", ".join(report["actions"]))
    if report.get("live"):
        lines.append(
            "会话内仅可 checkpoint；其他恢复或远端核实请退出后运行 repo-agent recover plan。"
        )
    lines.append("不重放工具，不恢复历史权限，不自动继续队列或合并。")
    return "\n".join(lines)
