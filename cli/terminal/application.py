"""Full-screen interaction orchestration; screen state belongs to the UI loop."""

import asyncio
from queue import Empty, SimpleQueue
from time import perf_counter
from uuid import uuid4

from prompt_toolkit.document import Document
from prompt_toolkit.utils import get_cwidth

from agent.transcript import Transcript
from host_support.cancellation import CancellationContext
from llm import LLMError

from ..cancellation import cancellation_notice
from ..conversation_help import HELP, RESET_NOTICE, describe_skills
from ..model_picker import ModelPicker
from ..output import LiveOutput
from ..runtime_events import RuntimeEventBridge
from ..sessions_command import new_name, ui_command
from ..shortcuts import shortcut_help, shortcut_label
from ..task_controller import TaskController
from ..task_execution import TaskRunner
from ..thinking_display import ThinkingDisplay
from .layout import build_layout


class ConversationUI:
    def __init__(
        self,
        runtime,
        *,
        sandbox=None,
        writeback="manual",
        thinking=None,
        status=None,
        models=None,
        display=None,
        conversation=None,
        terminal_input=None,
        terminal_output=None,
    ):
        self.runtime = runtime
        self.sandbox = sandbox
        self.writeback = writeback
        self.thinking = thinking
        self.status = status
        self.models = models
        self.display = display or ThinkingDisplay()
        self.conversation = conversation
        self.session_title = conversation.label if conversation else "会话"
        self.title_checked = perf_counter()
        self.renaming = False
        self.blocks = conversation.transcript if conversation else Transcript()
        self.thinking_lines = set()
        self.model_started_at = None
        self.thinking_characters = 0
        self.model_wizard = None
        self.model_picker = None
        self.history = conversation.history if conversation else ()
        self.busy = False
        self.cancellation = CancellationContext()
        self.cancelled = self.cancellation.event
        self.phase = "就绪"
        self.follow = True
        self.loop = None
        self.worker = None
        self._queued_ticket = None
        self._submission = None
        self.controller = TaskController(
            runtime,
            conversation=conversation,
            sandbox=sandbox,
            writeback=writeback,
            write=self.write,
            write_model=self.write_model,
            status=status,
        )
        self.transcript = ""
        self.pending_text = []
        self.worker_events = SimpleQueue()
        self.flush_handle = None
        self.footer_text = ""
        self.user_lines = {}
        build_layout(self, terminal_input=terminal_input, terminal_output=terminal_output)
        self.refresh_footer()
        self.append(
            (conversation.notice + "\n")
            if conversation
            else "欢迎。输入任务开始；/help 查看命令。\n"
        )

    def header_text(self):
        # External CLI renames become visible while idle without polling disk on
        # every frame. Titles are metadata only, never part of model context.
        if self.conversation and perf_counter() - self.title_checked > 1:
            try:
                self.session_title = self.conversation.label
            except (OSError, ValueError, KeyError):
                pass
            self.title_checked = perf_counter()
        mode = getattr(self.conversation, "mode", None)
        label = {
            "local": "local · 直接修改",
            "native": "native · 直接修改",
            "docker": "Docker · 工作副本",
        }.get(mode, "")
        return " CODING AGENT · " + self.session_title + (" · " + label if label else "")

    def footer_height(self):
        width = max(1, self.app.output.get_size().columns)
        return sum(
            max(1, (get_cwidth(line) + width - 1) // width) for line in self.footer_text.split("\n")
        )

    def footer_summary(self, status_text=None):
        model = self.models.describe().removesuffix("（/model 切换）") if self.models else ""
        thinking = self.thinking.describe(compact=True) if self.thinking else ""
        display = {"collapsed": "折叠", "expanded": "展开", "hidden": "隐藏"}[self.display.mode]
        first = " · ".join(x for x in (model, thinking, f"显示 {display}") if x)
        if status_text is None:
            status_text = self.status.describe(compact=True) if self.status else ""
        return "\n".join(x for x in (first, status_text) if x)

    def refresh_footer(self):
        if self.conversation:
            self.session_title = self.conversation.label
        self.footer_text = self.footer_summary()
        self.app.invalidate()

    def phase_text(self):
        text = " " + self.phase
        queue = self.controller.queue
        text += f" · 队列 {len(queue.pending)} 项" + ("（已暂停）" if queue.paused else "")
        if not self.follow:
            text += f" · 正在查看历史 · {shortcut_label('Ctrl+End')} 回到最新"
        if self.busy and self.model_started_at is not None:
            text += f" · 已等待 {perf_counter() - self.model_started_at:.1f}s"
            if self.thinking_characters:
                text += f" · 思考已接收 {self.thinking_characters:,} 字符"
        return text

    def history_page_size(self):
        info = self.chat.window.render_info
        return max(1, info.window_height - 1) if info else 10

    def follow_latest(self):
        self.follow = True
        self.chat.buffer.cursor_position = len(self.chat.text)
        self.app.invalidate()

    def scroll_history(self, rows):
        """Move by screen rows, including rows inside a wrapped paragraph."""
        window = self.chat.window
        info = window.render_info
        if info is None:
            return

        def shift(position, amount):
            line, offset = position
            offset += amount
            while offset < 0 and line > 0:
                line -= 1
                offset += info.get_height_for_line(line)
            while line < info.content_height - 1:
                height = info.get_height_for_line(line)
                if offset < height:
                    break
                offset -= height
                line += 1
            return line, max(0, min(offset, info.get_height_for_line(line) - 1))

        last = info.content_height - 1
        bottom = shift((last, info.get_height_for_line(last) - 1), -(info.window_height - 1))
        target = min(shift((window.vertical_scroll, window.vertical_scroll_2), rows), bottom)
        self.follow = False
        window.vertical_scroll, window.vertical_scroll_2 = target
        if rows > 0 and target == bottom:
            self.follow_latest()
        else:
            self.app.invalidate()

    def append(self, text, *, user=False):
        self.blocks.append(text, kind="user" if user else "agent")
        self.render_transcript()

    def render_transcript(self):
        self.transcript, users, thoughts = self.blocks.render(self.display.mode)
        self.user_lines.clear()
        self.user_lines.update(users)
        self.thinking_lines.clear()
        self.thinking_lines.update(thoughts)
        cursor = (
            len(self.transcript)
            if self.follow
            else min(self.chat.buffer.cursor_position, len(self.transcript))
        )
        self.chat.buffer.set_document(Document(self.transcript, cursor), bypass_readonly=True)
        self.app.invalidate()

    def write(self, *values, sep=" ", end="\n", kind="agent", **kwargs):
        text = sep.join(str(v) for v in values) + end
        self.dispatch(self.queue_content, kind, text)

    def dispatch(self, callback, *args):
        # Publish before scheduling the wakeup: task completion must be able to
        # drain output even when the UI has not yet handled that wakeup.
        self.worker_events.put((callback, args))
        self.loop.call_soon_threadsafe(self.drain_worker_events)

    def drain_worker_events(self):
        while True:
            try:
                callback, args = self.worker_events.get_nowait()
            except Empty:
                return
            callback(*args)

    def write_model(self, *values, **kwargs):
        self.write(*values, kind="text", **kwargs)

    def queue_text(self, text):
        self.queue_content("text", text)

    def queue_content(self, kind, text="", step=0, elapsed=0.0):
        self.pending_text.append((kind, text, step, elapsed))
        if kind == "thinking_delta":
            self.thinking_characters += len(text)
        if self.flush_handle is None:
            self.flush_handle = self.loop.call_later(0.05, self.flush_text)

    def flush_text(self):
        self.drain_worker_events()
        if self.flush_handle:
            self.flush_handle.cancel()
            self.flush_handle = None
        if self.pending_text:
            for kind, text, step, elapsed in self.pending_text:
                if kind in {"text", "agent"}:
                    self.blocks.append(text, kind=kind)
                else:
                    self.blocks.thinking(kind, text, step, elapsed)
            self.pending_text.clear()
            self.render_transcript()

    def display_command(self, task):
        try:
            self.display.command(task)
            self.flush_text()
            self.render_transcript()
            self.refresh_footer()
        except (ValueError, OSError, LLMError) as error:
            self.append(f"\n设置未变更：{error}\n")

    def submit(self):
        self._submit()
        if self.conversation and not self.busy:
            self.conversation.checkpoint(
                history=self.history, transcript=self.blocks, emit=self.append
            )

    def _submit(self):
        if self.renaming:
            try:
                notice = self.conversation.rename(self.editor.text)
            except (ValueError, OSError) as error:
                self.phase = str(error)
                return
            self.renaming = False
            self.editor.text = self.rename_draft
            self.append(notice + "\n")
            self.phase = "就绪"
            self.refresh_footer()
            return
        if self.model_wizard is not None:
            self.submit_model()
            return
        task = self.editor.text.strip()
        if not task:
            return
        if task.split()[:2] == ["/thinking", "display"]:
            self.editor.text = ""
            self.display_command(task)
            return
        if task.split()[0] == "/queue":
            try:
                self.append(self.controller.command(task) + "\n")
                self.editor.text = ""
                self._start_next()
            except (ValueError, OSError) as error:
                self.phase = str(error)
            return
        if not task.startswith("/"):
            if self._submission is None or self._submission[0] != task:
                self._submission = (task, uuid4().hex)
            try:
                ticket = self.controller.enqueue(task, submission_id=self._submission[1])
                self.editor.text = ""
                self._submission = None
                self.phase = f"已加入队列 #{ticket['number']}，当前任务不会收到这条消息"
                self._start_next()
            except (ValueError, OSError) as error:
                self.phase = str(error)
            return
        if self.busy:
            self.phase = "任务运行中；可输入普通任务排队，或使用 /queue 管理"
            return
        if task.split()[0] in {
            "/model",
            "/new",
            "/clear",
            "/apply",
            "/compact",
            "/thinking",
            "/context",
        }:
            try:
                self.controller.require_idle_configuration()
            except ValueError as error:
                self.phase = str(error)
                return
        self.editor.text = ""
        self.follow = True
        self.append("\n")
        self.append(f"你> {task}\n", user=True)
        if task in {"/exit", "/quit"}:
            self.app.exit()
            return
        if task == "/model" and self.models:
            try:
                self.model_wizard = self.models.wizard()
                self.phase = "设置模型 · Ctrl+C 取消"
                self.append(self.model_wizard.prompt())
            except (ValueError, OSError, LLMError) as error:
                self.append(f"无法读取模型配置：{error}\n")
            return
        if task.split()[0] in {"/rename", "/sessions", "/logs", "/ledger"} and self.conversation:
            try:
                self.append(ui_command(self.conversation, task) + "\n")
                self.refresh_footer()
            except (ValueError, OSError) as error:
                self.append(str(error) + "\n")
            return
        if task.split()[0] == "/new" and self.conversation:
            try:
                notice = self.conversation.new_session(name=new_name(task), transcript=self.blocks)
                self.history = ()
                self.blocks.blocks.clear()
                self.blocks.active_thinking = None
                self.conversation.transcript = self.blocks
                self.append(notice + "\n")
                self.refresh_footer()
            except (OSError, ValueError) as error:
                self.append(str(error) + "\n")
            return
        if task == "/clear":
            self.controller.reset_history()
            self.history = ()
            if self.conversation:
                self.conversation.clear(transcript=self.blocks, emit=self.append)
            if self.status:
                self.status.reset_context()
                self.refresh_footer()
            self.append(RESET_NOTICE + "\n")
            return
        if task == "/help":
            self.append(HELP + "\n" + shortcut_help() + "\n")
            return
        if task == "/skills":
            self.append(describe_skills(self.runtime) + "\n")
            return
        if task.split()[0] == "/context" and self.status:
            try:
                self.append(self.status.context_command(task) + "\n")
                self.refresh_footer()
            except (ValueError, OSError, LLMError) as error:
                self.append(f"设置未变更：{error}\n")
            return
        if task.split()[0] == "/thinking" and self.thinking:
            try:
                result = self.thinking.command(task)
                self.refresh_footer()
                self.append(result + "\n")
            except (ValueError, OSError, LLMError) as error:
                self.append(f"设置未变更：{error}\n")
            return
        if (
            task.startswith("/")
            and not (self.sandbox and task in {"/diff", "/apply"})
            and not (task == "/compact" and self.conversation)
        ):
            self.append("未知会话命令，请输入 /help。\n")
            return
        self.busy = True
        self.cancellation = CancellationContext()
        self.cancelled = self.cancellation.event
        self.phase = "开始执行…"
        self.worker = asyncio.create_task(self.execute(task))

    def _start_next(self):
        if self.busy or self.controller.closing:
            return
        try:
            ticket = self.controller.start_next()
        except (OSError, ValueError) as error:
            self.phase = str(error)
            self.append(str(error) + "\n")
            return
        if ticket is None:
            return
        self._queued_ticket = ticket
        self.history = self.controller.history
        if self.conversation:
            self.render_transcript()
        else:
            self.append(f"\n你> {ticket['text']}\n", user=True)
        self.busy = True
        self.cancellation = self.controller.cancellation
        self.cancelled = self.cancellation.event
        self.phase = f"正在执行 #{ticket['number']}"
        self.worker = asyncio.create_task(self.execute(ticket["text"]))

    def stop_task(self):
        self.cancelled.set()
        try:
            self.controller.stop()
        except (OSError, ValueError) as error:
            self.append(str(error) + "\n")

    async def _execute_queued(self, ticket):
        try:
            outcome = await asyncio.to_thread(self.work, ticket["text"])
            self.flush_text()
            state = self.controller.finish(ticket, outcome, transcript=self.blocks)
            self.history = self.controller.history
            if state != "completed":
                self.append("\n" + self.controller.queue.data["reason"] + "\n")
                self.phase = "队列已暂停；/queue 查看，/queue resume 继续"
            else:
                self.phase = "任务已完成"
        except Exception as error:
            self.phase = "队列已暂停：保存或调度失败"
            self.controller.persistence_blocked = True
            self.controller.queue.pause("任务收尾未确认，请检查会话状态后恢复")
            self.append(str(error) + "\n")
        finally:
            self.history = self.controller.history
            self._queued_ticket = None
            self.busy = False
            self.refresh_footer()
        self._start_next()

    def cancel_model(self):
        self.model_wizard = None
        self.model_picker = None
        self.editor.buffer.reset()
        self.phase = "就绪"
        self.append("\n模型切换已取消，原模型和上下文保留。\n")

    def submit_model(self):
        # Never append wizard inputs (especially keys) to chat history or task traces.
        value = self.editor.text
        try:
            if self.model_wizard.stage == "model":
                value = self.model_picker.value()
            selection = self.model_wizard.submit(value)
            self.editor.buffer.reset()
            if self.model_wizard.stage == "model":
                self.model_picker = ModelPicker(self.model_wizard.provider, self.model_wizard.model)
            if selection is not None:
                message = self.models.switch(selection)
                self.model_wizard = None
                self.model_picker = None
                self.history = ()
                self.controller.reset_history()
                self.phase = "就绪"
                self.append("\n" + message + "\n")
                self.refresh_footer()
                return
        except (ValueError, OSError, LLMError) as error:
            self.append(f"\n模型未切换：{error}\n")
        self.append("\n" + self.model_wizard.prompt())
        self.app.invalidate()

    def check_cancelled(self):
        self.cancellation.check()

    def work(self, task):
        if self._queued_ticket is not None:
            return self.controller.execute(self._queued_ticket)
        return TaskRunner(
            runtime=self.runtime,
            cancellation=self.cancellation,
            check_cancelled=self.check_cancelled,
            write=self.write,
            write_model=self.write_model,
            sandbox=self.sandbox,
            writeback_mode=self.writeback,
            conversation=self.conversation,
        ).run(task, history=self.history)

    async def execute(self, task):
        if self._queued_ticket is not None:
            return await self._execute_queued(self._queued_ticket)
        try:
            outcome = await asyncio.to_thread(self.work, task)
            kind, value = outcome
            self.flush_text()
            notice_text = cancellation_notice(value.report) if kind == "cancelled" else str(value)
            if task == "/compact":
                self.history = self.conversation.history
                if kind in {"error", "cancelled"}:
                    self.append(f"压缩未完成：{notice_text}\n")
            elif kind in {"error", "cancelled"}:
                if self.sandbox:
                    self.sandbox.guard.needs_review = True
                self.history = (
                    self.conversation.fail_task(
                        transcript=self.blocks,
                        emit=self.append,
                        cancellation=value.report if kind == "cancelled" else None,
                    )
                    if self.conversation
                    else ()
                )
                if self.status:
                    self.status.reset_context()
                notice = (
                    "此前完整上下文已保留；继续前请检查文件现状。"
                    if self.conversation
                    else RESET_NOTICE
                )
                self.append("\n" + notice_text + "\n" + notice + "\n")
            elif kind == "result":
                reset = value.status == "stopped" and not value.resumable
                self.history = (
                    self.conversation.finish_task(value, transcript=self.blocks, emit=self.append)
                    if self.conversation
                    else (() if reset else value.history)
                )
                if reset and self.status:
                    self.status.reset_context()
            self.phase = "就绪"
            if kind == "cancelled":
                self.phase = (
                    "已停止；进程清理未确认"
                    if value.report["cleanup_status"] == "unknown"
                    else "用户已停止，可重新输入"
                )
            elif kind == "error":
                self.phase = "任务执行失败，可重新输入"
            elif kind == "result" and value.status != "completed":
                self.phase = "任务尚未完成，可输入“继续”" if value.resumable else "任务未完成"
            if kind in {"error", "cancelled"}:
                self.controller.pause("会话操作未完成，请检查后 /queue resume")
            if outcome.cleanup_status == "unknown":
                self.controller.cleanup_blocked = True
                self.controller.pause("进程清理未确认")
        finally:
            try:
                if self.conversation:
                    self.conversation.checkpoint(
                        history=self.history, transcript=self.blocks, emit=self.append
                    )
            finally:
                self.busy = False
                self.refresh_footer()
        self._start_next()

    async def run_async(self):
        self.loop = asyncio.get_running_loop()
        bridge = RuntimeEventBridge(
            self.runtime,
            dispatch=self.dispatch,
            progress=self.progress,
            model_boundary=self.model_boundary,
            thinking=self.queue_content,
            status_text=lambda: self.status.describe(compact=True) if self.status else "",
            write_meta=self.write,
            live=LiveOutput(self.write_model, display=self.display, write_meta=self.write),
            check_cancelled=self.check_cancelled,
        )
        with bridge:
            try:
                await self.app.run_async()
            finally:
                self.controller.closing = True
                if self.controller.active or self.controller.queue.pending:
                    self.controller.queue.pause("会话已退出；恢复后请明确继续")
                self.cancelled.set()
                try:
                    if self.worker:
                        # Finish file operations before restoring runtime callbacks.
                        # Await finished tasks too, so cleanup failures are retrieved.
                        await asyncio.shield(self.worker)
                finally:
                    self.flush_text()
                    if self.conversation:
                        self.conversation.checkpoint(
                            history=self.history, transcript=self.blocks, emit=self.append
                        )

    def model_boundary(self, kind, step):
        if kind == "model_start":
            self.model_started_at = perf_counter()
            self.thinking_characters = 0
        else:
            self.flush_text()
            self.model_started_at = None

    def progress(self, phase, footer):
        if phase and not self.cancelled.is_set():
            self.phase = phase
        self.footer_text = self.footer_summary(footer)
        self.app.invalidate()

    def run(self):
        asyncio.run(self.run_async())
        # Preserve a readable final transcript in the terminal scrollback/session log.
        print(self.transcript)
        print(self.footer_text)
        print("会话已结束。")
