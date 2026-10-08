"""Single consumer for a session queue, shared by terminal and line interfaces."""

from copy import deepcopy
from threading import RLock

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
        return getattr(backend, "healthy", True)

    def resume(self):
        with self.lock:
            if self.closing or (self.active and self.cancellation.event.is_set()):
                raise ValueError("正在停止，请等待当前任务收尾")
            if self.cleanup_blocked or not self._healthy():
                raise ValueError("进程清理尚未确认；请退出、检查进程后重新启动")
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
                raise ValueError("进程清理尚未确认；请退出、检查进程后重新启动")
            executed = [t for t in self.queue.data["tasks"] if t["attempts"]]
            previous = max(executed, key=lambda t: t.get("finished_at", ""), default=None)
            if (
                previous is None
                or previous["state"] not in {"needs_input", "failed"}
                or previous["result"].get("run_status") != "max_steps"
                or previous["result"].get("cleanup_status") == "unknown"
                or not self.history
            ):
                raise ValueError("当前没有可继续的调用上限任务，或其上下文已清空")
            # Reuse the staged entry if a preceding save failed. Never duplicate it.
            task = next(
                (t for t in self.queue.pending if t.get("continuation_of") == previous["id"]),
                None,
            )
            if task is None:
                task = self.queue.add(
                    "继续完成上一项因调用轮数上限暂停的任务。"
                    "从已有上下文和工具结果继续，不要重复已经完成的操作。"
                )
                task["continuation_of"] = previous["id"]
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
            config = getattr(self.runtime.llm, "config", None)
            settings = {
                "model": model_identity(config) if config is not None else {},
                "max_steps": getattr(self.runtime, "max_steps", None),
                "max_output_tokens": getattr(self.runtime, "max_output_tokens", None),
                "thinking": deepcopy(getattr(self.runtime, "thinking_settings", {})),
                "mode": getattr(self.conversation, "mode", "local"),
                "access_mode": getattr(self.conversation, "access_mode", "develop"),
                "writeback": self.writeback,
            }
            task = self.queue.claim(settings)
            if task is None:
                return None
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
            return state

    def command(self, text):
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
        return "\n".join(lines)
