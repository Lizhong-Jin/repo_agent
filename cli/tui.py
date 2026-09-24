"""Persistent terminal application; only the UI loop mutates screen components."""

import asyncio
from threading import Event
from time import perf_counter

from prompt_toolkit import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, DynamicContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from llm import LLMError

from .sessions_command import new_name, ui_command
from .live import LiveOutput
from .model_picker import ModelPicker
from .shortcuts import shortcut_help, shortcut_label
from .thinking_display import ThinkingDisplay
from .transcript import Transcript


class ConversationLexer(Lexer):
    """Style by message origin, so quoted user labels in model output stay neutral."""

    def __init__(self, user_lines, thinking_lines=None, agent_lines=None):
        self.user_lines = user_lines
        self.agent_lines = agent_lines if agent_lines is not None else set()
        self.thinking_lines = thinking_lines if thinking_lines is not None else set()

    def lex_document(self, document):
        def line_fragments(line_number):
            text = document.lines[line_number]
            if line_number in self.thinking_lines:
                return [("class:thinking", text)]
            if line_number in self.agent_lines:
                return [("class:agent", text)]
            if line_number not in self.user_lines:
                return [("", text)]
            if self.user_lines[line_number]:
                return [("class:user-label", text[:3]), ("class:user-message", text[3:])]
            return [("class:user-message", text)]

        return line_fragments


class UserMessageWindow(Window):
    """Extend user-message backgrounds across actual rendered rows, including wraps."""

    def __init__(self, user_lines, *, following=lambda: True, scroll_history=None, **kwargs):
        super().__init__(**kwargs)
        self.user_lines = user_lines
        self.following = following
        self.scroll_history = scroll_history

    def _mouse_handler(self, mouse_event):
        if self.scroll_history is not None:
            if mouse_event.event_type == MouseEventType.SCROLL_UP:
                self.scroll_history(-3)
                return None
            if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
                self.scroll_history(3)
                return None
        return super()._mouse_handler(mouse_event)

    def _scroll(self, ui_content, width, height):
        if self.following():
            return super()._scroll(ui_content, width, height)
        # The editor keeps keyboard focus. While browsing history the viewport,
        # not the transcript's off-screen cursor, determines what stays visible.
        self.horizontal_scroll = 0
        self.vertical_scroll = min(self.vertical_scroll, max(0, ui_content.line_count - 1))
        line_height = ui_content.get_height_for_line(
            self.vertical_scroll, max(1, width), self.get_line_prefix
        )
        self.vertical_scroll_2 = min(self.vertical_scroll_2, max(0, line_height - 1))

    def _apply_style(self, new_screen, write_position, parent_style):
        super()._apply_style(new_screen, write_position, parent_style)
        if self.render_info is None:
            return
        for row, line in self.render_info.visible_line_to_input_line.items():
            if line in self.user_lines and 0 <= row < write_position.height:
                new_screen.fill_area(
                    WritePosition(
                        write_position.xpos, write_position.ypos + row, write_position.width, 1
                    ),
                    "class:user-row",
                    after=True,
                )


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
        self.cancelled = Event()
        self.phase = "就绪"
        self.follow = True
        self.loop = None
        self.worker = None
        self.transcript = ""
        self.pending_text = []
        self.flush_handle = None
        self.footer_text = ""
        self.user_lines = {}
        self.chat = TextArea(
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=True,
            style="class:chat",
            lexer=ConversationLexer(self.user_lines, self.thinking_lines, self.blocks.agent_lines),
        )
        self.chat.window = UserMessageWindow(
            self.user_lines,
            following=lambda: self.follow,
            scroll_history=self.scroll_history,
            content=self.chat.control,
            style="class:chat",
            wrap_lines=True,
            right_margins=self.chat.window.right_margins,
        )
        self.editor = TextArea(
            height=3,
            multiline=True,
            wrap_lines=True,
            prompt=lambda: (
                {"provider": "供应商> ", "model": "搜索> ", "key": "API Key> "}[
                    self.model_wizard.stage
                ] if self.model_wizard else "你> "
            ),
            password=Condition(lambda: self.model_wizard is not None and self.model_wizard.secret),
            style="class:editor",
            completer=WordCompleter(
                [
                    "/help",
                    "/clear",
                    "/compact",
                    "/new",
                    "/rename",
                    "/sessions",
                    "/logs",
                    "/exit",
                    "/model",
                    "/thinking",
                    "/context",
                    "/diff",
                    "/apply",
                    "/skills",
                ]
            ),
        )
        keys = KeyBindings()
        choosing_model = Condition(
            lambda: self.model_wizard is not None and self.model_wizard.stage == "model"
        )

        def search_model(_):
            if choosing_model() and self.model_picker:
                self.model_picker.search(self.editor.text)

        self.editor.buffer.on_text_changed += search_model

        @keys.add("up", filter=choosing_model)
        def model_up(event):
            self.model_picker.move(-1)

        @keys.add("down", filter=choosing_model)
        def model_down(event):
            self.model_picker.move(1)

        @keys.add("enter")
        def submit(event):
            self.submit()

        @keys.add("escape", "enter")
        def newline(event):
            self.editor.buffer.insert_text("\n")

        @keys.add("s-tab")
        def thinking(event):
            if self.model_wizard:
                return
            if self.busy:
                self.phase = "任务运行中；结束后可切换思考设置"
            elif self.thinking:
                try:
                    self.thinking.cycle()
                    self.refresh_footer()
                except (ValueError, OSError, LLMError) as error:
                    self.append(f"\n设置未变更：{error}\n")

        @keys.add("f2")
        def rename_session(event):
            if (self.conversation and not self.busy and not self.model_wizard
                    and not self.renaming):
                self.rename_draft = self.editor.text
                self.renaming = True
                self.editor.text = self.conversation.store.catalog.get(
                    self.conversation.store.id)["name"]
                self.editor.buffer.cursor_position = len(self.editor.text)
                self.phase = f"会话改名 · {shortcut_label('Enter')} 保存 · Ctrl+C 取消"

        @keys.add("c-t")
        def toggle_thinking(event):
            if self.model_wizard:
                return
            try:
                self.display.toggle()
                self.flush_text()
                self.render_transcript()
                self.refresh_footer()
            except (ValueError, OSError, LLMError) as error:
                self.append(f"\n设置未变更：{error}\n")

        @keys.add("c-c")
        def interrupt(event):
            if self.renaming:
                self.renaming = False
                self.editor.text = self.rename_draft
                self.phase = "改名已取消"
                return
            if self.model_wizard:
                self.cancel_model()
                return
            if self.busy:
                self.cancelled.set()
                self.phase = "正在停止；等待当前网络读取或工具安全结束…"
            else:
                self.editor.text = ""
                self.phase = "输入已清空；Ctrl+D 退出"

        @keys.add("c-d")
        def exit_app(event):
            if self.renaming:
                interrupt(event)
                return
            if self.model_wizard:
                self.cancel_model()
                return
            if self.busy:
                self.cancelled.set()
                self.phase = "正在停止；结束后再次 Ctrl+D 退出"
            elif not self.editor.text:
                self.app.exit()

        @keys.add("pageup")
        def up(event):
            if choosing_model():
                self.model_picker.move(-self.model_picker.page_size)
            else:
                self.scroll_history(-self.history_page_size())

        @keys.add("pagedown")
        def down(event):
            if choosing_model():
                self.model_picker.move(self.model_picker.page_size)
            else:
                self.scroll_history(self.history_page_size())

        @keys.add("c-end")
        @keys.add("escape", "g")
        def end(event):
            self.follow_latest()

        self.app = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(self.header_text),
                            height=1,
                            style="class:header",
                        ),
                        self.chat,
                        ConditionalContainer(
                            DynamicContainer(lambda: self.model_picker.window
                                             if self.model_picker else Window()),
                            filter=choosing_model,
                        ),
                        Window(
                            FormattedTextControl(self.phase_text),
                            height=1,
                            style="class:status",
                        ),
                        Frame(
                            self.editor,
                            title=lambda: (
                                "模型设置 · Enter 确认 · Ctrl+C 取消" if self.model_wizard else
                                f"{shortcut_label('Enter')} 发送 · "
                                f"{shortcut_label('Alt+Enter')} 换行 · Tab 补全"
                            ),
                        ),
                        Window(
                            FormattedTextControl(lambda: self.footer_text),
                            height=self.footer_height,
                            wrap_lines=True,
                            style="class:footer",
                        ),
                        Window(
                            FormattedTextControl(
                                f" {shortcut_label('F2')} 改名 · Shift+Tab 强度 · "
                                "Ctrl+T 思考显示 · Ctrl+C 停止 · "
                                f"滚轮/{shortcut_label('PgUp')}/{shortcut_label('PgDn')} 历史"
                            ),
                            height=1,
                            style="class:hint",
                        ),
                    ]
                ),
                focused_element=self.editor,
            ),
            key_bindings=keys,
            full_screen=True,
            mouse_support=True,
            refresh_interval=0.1,
            input=terminal_input,
            output=terminal_output,
            style=Style.from_dict(
                {
                    "header": "bg:#243447 #ffffff bold",
                    "chat": "#e6e6e6",
                    "agent": "#819bb0",
                    "thinking": "#999999 italic",
                    "editor": "#ffffff",
                    "user-row": "bg:#20364d",
                    "user-message": "bg:#20364d #e5f2ff",
                    "user-label": "bg:#20364d #81c7ff bold",
                    "status": "#819bb0",
                    "footer": "bg:#171d25 #8193a3",
                    "hint": "#78838e",
                }
            ),
        )
        self.refresh_footer()
        self.append((conversation.notice + "\n") if conversation else
                    "欢迎。输入任务开始；/help 查看命令。\n")

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
        self.loop.call_soon_threadsafe(self.queue_content, kind, text)

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
        from .interactive import HELP, RESET_NOTICE, describe_skills

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
        if self.busy:
            self.phase = "任务运行中，输入已保留；完成后按 Enter 发送"
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
        if task.split()[0] in {"/rename", "/sessions", "/logs"} and self.conversation:
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
        if task.startswith("/") and not (self.sandbox and task in {"/diff", "/apply"}) and not (task == "/compact" and self.conversation):
            self.append("未知会话命令，请输入 /help。\n")
            return
        if self.conversation and task not in {"/diff", "/apply", "/compact"}:
            try:
                self.conversation.start_task(task, transcript=self.blocks)
            except OSError as error:
                self.append(str(error) + "\n")
                return
        self.busy = True
        self.cancelled.clear()
        self.phase = "开始执行…"
        self.worker = asyncio.create_task(self.execute(task))

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
                self.phase = "就绪"
                self.append("\n" + message + "\n")
                self.refresh_footer()
                return
        except (ValueError, OSError, LLMError) as error:
            self.append(f"\n模型未切换：{error}\n")
        self.append("\n" + self.model_wizard.prompt())
        self.app.invalidate()

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise KeyboardInterrupt()

    def work(self, task):
        from .interactive import finish_writeback

        try:
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
            if self.sandbox and self.writeback == "on-success":
                self.sandbox.begin_task()
            result = self.runtime.run(task, history=self.history)
            self.check_cancelled()
            if result.undisplayed_text:
                self.write_model(result.undisplayed_text)
            if result.status != "completed":
                self.write(result.notice or f"任务尚未正常完成：{result.status}")
            finish_writeback(self.sandbox, result, self.writeback, emit=self.write)
            return ("result", result)
        except KeyboardInterrupt:
            return ("error", "当前任务已中断；已显示的输出可能不完整。")
        except Exception as error:
            return ("error", f"{type(error).__name__}: {error}")

    async def execute(self, task):
        from .interactive import RESET_NOTICE

        try:
            kind, value = await asyncio.to_thread(self.work, task)
            self.flush_text()
            if task == "/compact":
                self.history = self.conversation.history
                if kind == "error":
                    self.append(f"压缩未完成：{value}\n")
            elif kind == "error":
                if self.sandbox:
                    self.sandbox.guard.needs_review = True
                self.history = (
                    self.conversation.fail_task(transcript=self.blocks, emit=self.append)
                    if self.conversation else ()
                )
                if self.status:
                    self.status.reset_context()
                notice = ("此前完整上下文已保留；继续前请检查文件现状。"
                          if self.conversation else RESET_NOTICE)
                self.append("\n" + value + "\n" + notice + "\n")
            elif kind == "result":
                reset = value.status == "stopped" and not value.resumable
                self.history = (
                    self.conversation.finish_task(value, transcript=self.blocks, emit=self.append)
                    if self.conversation else (() if reset else value.history)
                )
                if reset and self.status:
                    self.status.reset_context()
            self.phase = "就绪"
            if kind == "error":
                self.phase = "任务未完成，可重新输入"
            elif kind == "result" and value.status != "completed":
                self.phase = "任务尚未完成，可输入“继续”" if value.resumable else "任务未完成"
        finally:
            if self.conversation:
                self.conversation.checkpoint(history=self.history, transcript=self.blocks,
                                             emit=self.append)
            self.busy = False
            self.refresh_footer()

    async def run_async(self):
        self.loop = asyncio.get_running_loop()
        previous = (
            self.runtime.on_event,
            self.runtime.on_model_event,
            self.runtime.check_cancelled,
        )
        live = LiveOutput(self.write_model, display=self.display, write_meta=self.write)

        def event(name, stats):
            if previous[0]:
                previous[0](name, stats)
            # Snapshot mutable counters before passing them to the UI thread.
            footer = self.status.describe(compact=True) if self.status else ""
            record = stats.model_calls[-1] if stats.model_calls else None
            phase = f"模型 #{record.step} · 等待响应…" if name == "model_start" else None
            if name == "tool_start":
                phase = f"工具 · {stats.tool_calls[-1].name} 执行中…"
            if name == "recovery":
                phase = stats.recoveries[-1]["message"]
                self.write(f"[{phase}]")
            compact_call = record is not None and getattr(record, "purpose", "task") == "compaction"
            if compact_call and name == "model_start":
                phase = "正在生成历史摘要…"
            if name.startswith("compaction_"):
                phase = stats.compaction["phase"]
                self.write(f"[{phase}]")
            if name == "skill_loaded":
                self.write(f"[已加载技能：{stats.skill_loads[-1]['name']}]")
            self.loop.call_soon_threadsafe(self.progress, phase, footer)
            if name in {"model_start", "model_end"} and not compact_call:
                self.loop.call_soon_threadsafe(self.model_boundary, name, record)

        last_phase = None

        def model_event(kind, text, elapsed, record):
            nonlocal last_phase
            phase = None
            if kind == "first_data" and record.first_text_seconds is None:
                phase = "已收到数据，等待正文…"
            elif kind == "thinking_start":
                phase = "正在思考…"
            elif kind == "thinking_end":
                phase = "思考片段已接收，等待后续输出…"
            elif kind == "first_text" or kind == "text":
                phase = "正在输出…"
            if phase and last_phase != (id(record), phase):
                last_phase = (id(record), phase)
                footer = self.status.describe(compact=True) if self.status else ""
                self.loop.call_soon_threadsafe(
                    self.progress, f"模型 #{record.step} · {phase}", footer
                )
            if kind in {"thinking_start", "thinking_delta", "thinking_end"}:
                self.loop.call_soon_threadsafe(self.queue_content, kind, text, record.step, elapsed)
                return None
            return live(kind, text, elapsed, record)

        self.runtime.on_event = event
        self.runtime.on_model_event = model_event
        self.runtime.check_cancelled = self.check_cancelled
        try:
            await self.app.run_async()
        finally:
            self.cancelled.set()
            if self.worker and not self.worker.done():
                # Do not leave file operations running after the terminal has closed.
                await asyncio.shield(self.worker)
            self.flush_text()
            if self.conversation:
                self.conversation.checkpoint(history=self.history, transcript=self.blocks,
                                             emit=self.append)
            self.runtime.on_event, self.runtime.on_model_event, self.runtime.check_cancelled = (
                previous
            )

    def model_boundary(self, kind, record):
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
