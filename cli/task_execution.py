"""Blocking task execution with injected output and cancellation, without UI state."""

from collections.abc import Callable
from dataclasses import dataclass

from agent import AgentRuntime
from host_support.cancellation import RunCancelled, cancellation_scope, current_cancellation

from .writeback import finish_writeback


@dataclass
class TaskOutcome:
    kind: str
    value: object
    writeback_ok: bool = True
    cleanup_status: str = "not_needed"
    execution_report: dict | None = None

    def __iter__(self):
        # Existing single-task callers may still unpack kind/value.
        yield self.kind
        yield self.value


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
            if self.sandbox is not None:
                self.sandbox.last_verification = None
            outcome = self._run(task, history=history)
            if not isinstance(outcome, TaskOutcome):
                outcome = TaskOutcome(*outcome)
            outcome.execution_report = current_cancellation().report()
            outcome.execution_report.pop("status", None)
            outcome.cleanup_status = outcome.execution_report["cleanup_status"]
            return outcome

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
            written = finish_writeback(self.sandbox, result, self.writeback_mode, emit=self.write)
            return TaskOutcome("result", result, writeback_ok=written)
        except (RunCancelled, KeyboardInterrupt):
            context = current_cancellation()
            context.cancel()
            return ("cancelled", RunCancelled(context))
        except Exception as error:
            return ("error", f"{type(error).__name__}: {error}")
