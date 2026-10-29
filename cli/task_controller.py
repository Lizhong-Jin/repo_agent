"""Single consumer for a session queue, shared by terminal and line interfaces."""

import json
from copy import deepcopy
from threading import RLock

from agent import delivery
from agent.conversation import model_identity
from agent.task_queue import TaskQueue
from agent.task_reports import ReportCapture, render_report
from agent.transcript import display_text
from host_support.cancellation import CancellationContext, cancellation_scope

from .cancellation import cancellation_notice
from .task_execution import TaskOutcome, TaskRunner


class TaskController:
    def __init__(
        self,
        runtime,
        *,
        conversation=None,
        sandbox=None,
        writeback="manual",
        write=print,
        write_model=print,
        status=None,
    ):
        self.runtime = runtime
        self.conversation = conversation
        self.workspace = getattr(conversation, "workspace", None)
        self.sandbox = sandbox
        self.writeback = writeback
        self.write, self.write_model = write, write_model
        self.status = status
        self._queue = TaskQueue()
        self._history = ()
        self.lock = conversation.state_lock if conversation else RLock()
        self.active = None
        self.cancellation = CancellationContext()
        self.persistence_blocked = False
        self.cleanup_blocked = False
        self.closing = False
        self.report_capture = None
        self.report_error = None

    @property
    def delivery_policy(self):
        return getattr(self.conversation, "delivery_policy", delivery.DEFAULT)

    def delivery_result(self):
        if self.conversation is None or self.delivery_policy["mode"] == "report-only":
            return {"state": "report-only", "reasons": []}
        return delivery.inspect(
            self.conversation.store,
            delivery.latest_task(self.queue.data),
            self.delivery_policy,
            self.conversation.execution_root,
            self.workspace,
        )

    def delivery_allowed(self):
        if not delivery.latest_task(self.queue.data):
            return True
        return delivery.allowed(self.delivery_result())

    def delivery_command(self, text):
        parts = text.split(maxsplit=2)
        action = parts[1] if len(parts) > 1 else "status"
        argument = parts[2] if len(parts) > 2 else ""
        with self.lock:
            if action == "status" and not argument:
                return delivery.describe(self.delivery_result())
            if self.active or self.closing:
                raise ValueError("请等待当前任务及收尾结束后操作交付")
            if self.delivery_policy["mode"] != "strict":
                raise ValueError("当前为 report-only；请新建 strict 会话并指定必需检查")
            if self.cleanup_blocked or not self._healthy():
                raise ValueError("进程清理未确认；不能重新验证或接受例外，请先恢复执行环境")
            previous = delivery.latest_task(self.queue.data)
            if previous is None:
                raise ValueError("尚无已执行任务")
            if action in {"refresh", "accept-exception"}:
                if (action == "refresh" and argument) or (
                    action == "accept-exception" and not argument
                ):
                    raise ValueError("用法：/delivery refresh 或 /delivery accept-exception 原因")
                ref = delivery.publish(
                    self.conversation.store,
                    previous,
                    self.delivery_policy,
                    self.conversation.execution_root,
                    self.workspace,
                    exception=argument if action == "accept-exception" else None,
                )
                previous["result"]["delivery"] = ref
                self._save()
                return (
                    delivery.describe(self.delivery_result())
                    + "\n未自动恢复队列或合并；核实后 /queue resume。"
                )
            if action == "recheck" and not argument:
                content = (
                    "重新验证当前工作区：执行全部用户固定的必需命令检查，不修改项目代码。"
                    "人工项仍交由用户验收。"
                )
            elif action == "repair" and argument:
                content = argument
            else:
                raise ValueError(
                    "用法：/delivery [status|refresh|recheck|repair 文本|accept-exception 原因]"
                )
            staged = next(
                (t for t in self.queue.pending if t.get("delivery_recovery_of") == previous["id"]),
                None,
            )
            if staged is None:
                staged = self.queue.add(content)
                staged["delivery_recovery_of"] = previous["id"]
            elif staged["text"] != content:
                raise ValueError("已有交付修复项，请先处理或移除该项")
            self.queue.move(staged["number"], 1)
            self.queue.resume()
            self._save()
            return (
                f"已创建交付验证/修复任务 #{staged['number']}，使用模型和现有工具；"
                "必需检查不变，旧报告保留。"
            )

    @property
    def queue(self):
        return self.conversation.queue if self.conversation else self._queue

    @property
    def history(self):
        return self.conversation.history if self.conversation else self._history

    def reset_history(self):
        self._history = ()

    def _save(self):
        if self.conversation:
            try:
                self.conversation.checkpoint(strict=True)
            except (OSError, ValueError):
                self.persistence_blocked = True
                self.queue.pause("队列保存未确认；请修复存储后 /queue resume")
                raise
        self.persistence_blocked = False

    def enqueue(self, text, *, submission_id=None):
        with self.lock:
            task = self.queue.add(text, submission_id=submission_id)
            self._save()  # Acknowledgement follows durable storage, never the reverse.
            return deepcopy(task)

    def pause(self, reason="用户暂停队列"):
        with self.lock:
            self.queue.pause(reason)
            self._save()

    def stop(self):
        # Signal first: a disk failure must not prevent cancellation.
        self.cancellation.cancel()
        self.pause("用户停止当前任务；队列已暂停")

    def _healthy(self):
        backend = getattr(self.conversation, "execution_backend", None) or getattr(
            self.sandbox, "backend", None
        )
        return not getattr(self.conversation, "recovery_blocked", False) and getattr(
            backend, "healthy", True
        )

    def resume(self):
        with self.lock:
            if self.closing or (self.active and self.cancellation.event.is_set()):
                raise ValueError("正在停止，请等待当前任务收尾")
            if self.cleanup_blocked or not self._healthy():
                raise ValueError("进程清理尚未确认；请退出后运行 repo-agent recover plan")
            if not self.delivery_allowed():
                raise ValueError(
                    delivery.describe(self.delivery_result())
                    + "；使用 /delivery 处理，不能直接恢复队列"
                )
            self.queue.resume()
            self._save()

    def require_idle_configuration(self):
        if self.active or self.queue.pending:
            raise ValueError("请先结束当前任务并处理待执行任务；/queue clear 可移除待执行项")

    def continue_task(self):
        """Resume the latest step-limited run with fresh budget, ahead of waiting work."""
        with self.lock:
            if self.active or self.closing:
                raise ValueError("请等待当前任务收尾后再使用 /continue")
            if self.cleanup_blocked or not self._healthy():
                raise ValueError("进程清理尚未确认；请退出后运行 repo-agent recover plan")
            permissions = getattr(getattr(self.runtime, "access_policy", None), "permissions", None)
            if permissions is not None and permissions.pending:
                raise ValueError("请先 /permissions approve ID 或 deny ID 处理申请")
            executed = [t for t in self.queue.data["tasks"] if t["attempts"]]
            previous = max(executed, key=lambda t: t.get("finished_at", ""), default=None)
            if (
                previous is None
                or previous["state"] not in {"needs_input", "failed"}
                or (
                    previous["result"].get("run_status") != "max_steps"
                    and previous["result"].get("pause_reason") != "permission_required"
                )
                or previous["result"].get("cleanup_status") == "unknown"
                or not self.history
            ):
                raise ValueError("当前没有可继续的调用上限任务或权限暂停任务，或其上下文已清空")
            # Reuse the staged entry if a preceding save failed. Never duplicate it.
            task = next(
                (t for t in self.queue.pending if t.get("continuation_of") == previous["id"]),
                None,
            )
            if task is None:
                task = self.queue.add(
                    "继续完成上一项暂停的任务，遵守当前有效权限。"
                    "从已有上下文和工具结果继续，不要重复已经完成的操作。"
                )
                task["continuation_of"] = previous["id"]
                if self.delivery_policy["mode"] == "strict":
                    task["delivery_recovery_of"] = previous["id"]
            self.queue.move(task["number"], 1)
            self.queue.resume()
            self._save()
            budget = self.runtime.max_steps
            allowance = f"本次最多再调用模型 {budget} 轮" if budget else "当前配置为轮数无上限"
            return f"继续任务 #{previous['number']}（续接 #{task['number']}）；{allowance}"

    def start_next(self):
        with self.lock:
            if (
                self.active
                or self.closing
                or self.persistence_blocked
                or self.queue.paused
                or not self.queue.pending
            ):
                return None
            if not self._healthy() or self.cleanup_blocked:
                self.pause("执行环境或进程清理未确认")
                return None
            permissions = getattr(getattr(self.runtime, "access_policy", None), "permissions", None)
            if permissions is not None and permissions.pending:
                self.pause("存在待处理权限申请；请先 /permissions approve ID 或 deny ID")
                return None
            previous = delivery.latest_task(self.queue.data)
            recovery = self.queue.pending[0].get("delivery_recovery_of")
            if previous is not None and recovery != previous["id"] and not self.delivery_allowed():
                self.pause(delivery.describe(self.delivery_result()))
                return None
            config = getattr(self.runtime.llm, "config", None)
            settings = {
                "model": model_identity(config) if config is not None else {},
                "max_steps": getattr(self.runtime, "max_steps", None),
                "max_output_tokens": getattr(self.runtime, "max_output_tokens", None),
                "thinking": deepcopy(getattr(self.runtime, "thinking_settings", {})),
                "mode": getattr(self.conversation, "mode", "local"),
                "access_mode": getattr(self.conversation, "access_mode", "develop"),
                "writeback": self.writeback,
                "delivery_policy": deepcopy(self.delivery_policy),
            }
            task = self.queue.claim(settings)
            if task is None:
                return None
            previous_delivery = self.queue.data.get("delivery_latest")
            if self.delivery_policy["mode"] == "strict":
                self.queue.data["delivery_latest"] = task["id"]
            self.cancellation = CancellationContext()
            try:
                if self.conversation:
                    self.conversation.start_task(task["text"])
                else:
                    self._save()
                if self.workspace is not None:
                    self.workspace.begin_run(task["id"])
            except (OSError, ValueError):
                # No executor has been called. Keep the request available, but
                # require an explicit save/resume before trying to start again.
                task.update(state="queued", attempts=[])
                if self.delivery_policy["mode"] == "strict":
                    self.queue.data["delivery_latest"] = previous_delivery
                self.persistence_blocked = True
                self.queue.pause("启动状态保存未确认；尚未执行，请修复后 /queue resume")
                raise
            self.active = task
            return deepcopy(task)

    def execute(self, task):
        with cancellation_scope(self.cancellation, handle_sigint=True):
            return self._execute(task)

    def _execute(self, task):
        if not self.active or self.active["id"] != task["id"]:
            raise ValueError("任务不是当前执行项")
        self.report_capture = None
        self.report_error = None
        if self.conversation:
            try:
                self.report_capture = ReportCapture(
                    self.conversation, task, self.sandbox, self.writeback
                )
            except (OSError, ValueError) as error:
                self.report_error = f"报告基线保存失败：{error}"
                self.write(self.report_error)
        self.runtime.task_reporter = self.report_capture
        if self.delivery_policy["mode"] == "strict" and (
            self.report_error or not self.report_capture.report["baseline"]["complete"]
        ):
            return TaskOutcome(
                "error", self.report_error or "严格交付基线不完整，尚未调用模型或执行工具"
            )
        return TaskRunner(
            self.runtime,
            self.cancellation.check,
            self.write,
            self.write_model,
            sandbox=self.sandbox,
            writeback_mode=self.writeback,
            conversation=self.conversation,
            cancellation=self.cancellation,
        ).run(task["text"], history=self.history)

    def finish(self, task, outcome, *, transcript=None):
        with self.lock:
            if not self.active or self.active["id"] != task["id"]:
                raise ValueError("任务结果已处理或不属于当前执行项")
            if not isinstance(outcome, TaskOutcome):
                outcome = TaskOutcome(*outcome)
            kind, value = outcome
            summary = {
                "cleanup_status": outcome.cleanup_status,
                "writeback_ok": outcome.writeback_ok,
                "execution": outcome.execution_report or {},
            }
            if kind == "result":
                state = "completed" if value.status == "completed" else "needs_input"
                summary.update(
                    run_status=value.status,
                    notice=value.notice or "",
                    task_id=getattr(value.stats, "task_id", None),
                )
                if getattr(value, "pause_reason", ""):
                    summary["pause_reason"] = value.pause_reason
                if self.conversation:
                    self.conversation.finish_task(
                        value, transcript=transcript, emit=self.write, save=False
                    )
                else:
                    reset = value.status == "stopped" and not value.resumable
                    self._history = () if reset else value.history
                    if reset and self.status:
                        self.status.reset_context()
                if not outcome.writeback_ok:
                    state = "failed"
                    summary["notice"] = "自动回写未完成；队列已暂停，请检查副本与原文件"
            else:
                state = "cancelled" if kind == "cancelled" else "failed"
                summary["notice"] = (
                    cancellation_notice(value.report) if kind == "cancelled" else str(value)
                )
                if kind == "cancelled":
                    summary["cancellation"] = value.report
                if self.sandbox:
                    self.sandbox.guard.needs_review = True
                if self.conversation:
                    self.conversation.fail_task(
                        transcript=transcript,
                        emit=self.write,
                        save=False,
                        cancellation=value.report if kind == "cancelled" else None,
                    )
                else:
                    self._history = ()
                self.write(
                    "此前完整上下文已保留；已经执行的文件操作不会撤销，请检查文件现状。"
                    if self.conversation
                    else "上下文已清空；已经执行的文件操作不会撤销。"
                )
                if self.status:
                    self.status.reset_context()
            if outcome.cleanup_status == "unknown" or not self._healthy():
                self.cleanup_blocked = True
                if state == "completed":
                    state = "failed"
                # Cleanup uncertainty supplements the task's cause, never replaces it.
                summary["notice"] = "\n".join(
                    filter(None, (summary["notice"], "进程清理未确认；队列已暂停"))
                )
            report_notice = None
            self.runtime.task_reporter = None
            if self.report_capture is not None:
                try:
                    path = self.report_capture.finish(state, summary)
                    summary["report"] = str(path)
                    report_notice = display_text(render_report(self.report_capture.report))
                except (OSError, ValueError) as error:
                    self.report_error = f"任务报告未完整保存：{error}；/report 可查看已保存证据"
            if self.report_error:
                summary["report_error"] = self.report_error
                self.write(self.report_error)
            if self.delivery_policy["mode"] == "strict":
                try:
                    summary["delivery"] = delivery.publish(
                        self.conversation.store,
                        self.active,
                        self.delivery_policy,
                        self.conversation.execution_root,
                        self.workspace,
                        finishing=True,
                    )
                except (OSError, ValueError) as error:
                    summary["delivery_error"] = str(error)
                    self.queue.pause("交付结论保存失败；请修复存储后 /delivery refresh")
                if summary.get("delivery", {}).get("state") != "passed":
                    self.queue.pause("严格交付未通过；/delivery 查看，recheck 重验或 repair 修复")
            self.queue.finish(self.active, state, summary)
            self.active = None
            self._save()  # History, pending marker and queue result share one snapshot.
            if self.workspace is not None:
                try:
                    self.workspace.finish_run(
                        "unknown" if self.cleanup_blocked else outcome.cleanup_status
                    )
                except (OSError, ValueError):
                    self.persistence_blocked = True
                    self.queue.pause("工作区收尾状态保存失败；请退出后核实")
                    raise
            if report_notice:
                self.write(report_notice)
            if self.delivery_policy["mode"] == "strict":
                self.write(delivery.describe(self.delivery_result()))
            return state

    def recovery_command(self, text):
        from agent.recovery import Recovery, describe

        if self.conversation is None:
            raise ValueError("恢复入口需要已保存会话")
        parts = text.split()
        with self.lock:
            recovery = Recovery(
                self.conversation.store, self.conversation.store.id, controller=self
            )
            if parts in (["/recover"], ["/recover", "status"], ["/recover", "plan"]):
                return describe(recovery.inspect())
            if len(parts) == 4 and parts[1] == "apply" and parts[3] == "checkpoint":
                return describe(recovery.apply(parts[2], "checkpoint"))
            raise ValueError(
                "用法：/recover [status|plan] 或 /recover apply 计划编号 checkpoint；"
                "其他恢复请退出后操作"
            )

    def command(self, text):
        if text.split()[:1] == ["/recover"]:
            return self.recovery_command(text)
        if text.split()[:1] == ["/permissions"]:
            return self.permissions_command(text)
        if text.split()[:1] == ["/delivery"]:
            return self.delivery_command(text)
        parts = text.strip().split(maxsplit=2)
        if parts[:1] == ["/continue"]:
            if len(parts) != 1:
                raise ValueError("用法：/continue（使用当前 max_steps 预算）")
            return self.continue_task()
        action = parts[1] if len(parts) > 1 else "list"
        argument = parts[2] if len(parts) > 2 else ""
        if action == "list" and not argument:
            return self.describe()
        with self.lock:
            if action == "pause" and not argument:
                self.pause()
            elif action == "resume" and not argument:
                self.resume()
            elif action == "add" and argument:
                task = self.enqueue(argument)
                return f"已加入队列 #{task['number']}"
            elif action == "remove" and argument:
                self.queue.remove(argument)
                self._save()
            elif action == "edit" and argument:
                number, content = argument.split(maxsplit=1)
                self.queue.edit(number, content)
                self._save()
            elif action == "move" and argument:
                number, position = argument.split()
                self.queue.move(number, int(position))
                self._save()
            elif action == "clear" and not argument:
                for task in self.queue.pending:
                    self.queue.remove(task["number"])
                self._save()
            elif action == "retry" and argument:
                old = self.queue.get(argument)
                if old["state"] in {"queued", "running", "completed"}:
                    raise ValueError("只能重试未完成的历史任务")
                task = self.queue.add(old["text"], retry_of=old["id"])
                self._save()
                return f"已创建重试任务 #{task['number']}；使用 /queue resume 开始"
            else:
                raise ValueError(
                    "用法：/queue [list|add 文本|pause|resume|edit 编号 文本|"
                    "remove 编号|move 编号 位置|clear|retry 编号]"
                )
        return self.describe()

    def permissions_command(self, text):
        permissions = getattr(getattr(self.runtime, "access_policy", None), "permissions", None)
        if permissions is None:
            raise ValueError("当前运行环境没有权限管理器")
        parts = text.split()
        with self.lock, permissions.lock:
            if len(parts) == 1 or parts[1:] == ["list"]:
                state = permissions.snapshot()
                state.pop("events")
                return json.dumps(state, ensure_ascii=False, indent=2)
            if len(parts) != 3 or parts[1] not in {"approve", "deny", "revoke"}:
                raise ValueError(
                    "用法：/permissions [list|approve ID|deny ID|revoke ID|revoke all]"
                )
            if self.active or self.closing:
                raise ValueError("请等待当前任务和清理完成后再修改权限")
            if self.cleanup_blocked or not self._healthy():
                raise ValueError("进程清理未确认，不能修改权限；请退出检查")
            profile, pending, events = (
                permissions.profile,
                deepcopy(permissions.pending),
                permissions.events,
            )
            try:
                if parts[1] == "revoke":
                    permissions.revoke(parts[2])
                else:
                    permissions.decide(parts[2], approve=parts[1] == "approve")
                self._save()
            except (OSError, ValueError):
                if parts[1] == "approve":
                    permissions.profile, permissions.pending, permissions.events = (
                        profile,
                        pending,
                        events,
                    )
                raise
        return "权限已更新；仅当前会话有效。/continue 继续任务，或 /permissions 查看。"

    def describe(self):
        labels = {
            "queued": "等待",
            "running": "执行中",
            "completed": "完成",
            "cancelled": "已取消",
            "failed": "失败",
            "interrupted": "中断",
            "needs_input": "待处理",
        }
        state = "已暂停" if self.queue.paused else "可执行"
        lines = [f"队列{state} · 等待 {len(self.queue.pending)} 项"]
        if self.queue.data["reason"]:
            lines.append(self.queue.data["reason"])
        tasks = [t for t in self.queue.data["tasks"] if t["state"] in {"queued", "running"}]
        terminal = [t for t in self.queue.data["tasks"] if t not in tasks][-10:]
        for task in [*tasks, *terminal]:
            preview = task["text"].replace("\n", " ")[:80]
            lines.append(f"#{task['number']} [{labels[task['state']]}] {preview}")
            if (
                task["attempts"]
                and task["attempts"][-1]["config"].get("delivery_policy", {}).get("mode")
                == "strict"
            ):
                saved_state = task["result"].get("delivery", {}).get("state", "unknown")
                lines.append("  保存的交付结论：" + delivery.LABELS.get(saved_state, "无法确认"))
        return "\n".join(lines)
