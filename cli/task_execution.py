"""Blocking task execution with injected output and cancellation, without UI state."""

from collections.abc import Callable
from dataclasses import dataclass

from agent import AgentRuntime
from host_support.cancellation import RunCancelled, cancellation_scope, current_cancellation

from .writeback import finish_writeback


@dataclass
class TaskRunner:
    """One task boundary. Callers own history adoption and session checkpoints."""

    runtime: AgentRuntime
    check_cancelled: Callable[[], None]
    write: Callable
    write_model: Callable
    sandbox: object = None
    writeback_mode: str = "manual"
    conversation: object = None
    cancellation: object = None

    def run(self, task, *, history=()):
        with cancellation_scope(self.cancellation, handle_sigint=True):
            return self._run(task, history=history)

    def _run(self, task, *, history=()):
        try:
            current_cancellation().check()
            if task == "/compact":
                self.write(self.conversation.compact())
                return ("compact", None)
            if task == "/diff":
                self.write(self.sandbox.diff())
                return ("command", None)
            if task == "/apply":
                self.check_cancelled()
                self.write(f"已回写 {len(self.sandbox.apply())} 个文件。")
                if self.sandbox.last_backup:
                    self.write(f"回写前备份：{self.sandbox.last_backup}")
                return ("command", None)
            if self.sandbox and self.writeback_mode == "on-success":
                self.sandbox.begin_task()
            result = self.runtime.run(task, history=history)
            self.check_cancelled()
            if result.undisplayed_text:
                self.write_model(result.undisplayed_text)
            if result.status != "completed":
                self.write(result.notice or f"任务尚未正常完成：{result.status}")
            finish_writeback(self.sandbox, result, self.writeback_mode, emit=self.write)
            return ("result", result)
        except (RunCancelled, KeyboardInterrupt):
            context = current_cancellation()
            context.cancel()
            return ("cancelled", RunCancelled(context))
        except Exception as error:
            return ("error", f"{type(error).__name__}: {error}")
