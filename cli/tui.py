"""Persistent terminal application; only the UI loop mutates screen components."""

import asyncio
from threading import Event

from prompt_toolkit import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from llm import LLMError

from .live import LiveOutput


class ConversationLexer(Lexer):
    """Style by message origin, so quoted user labels in model output stay neutral."""

    def __init__(self, user_lines):
        self.user_lines = user_lines

    def lex_document(self, document):
        def line_fragments(line_number):
            text = document.lines[line_number]
            if line_number not in self.user_lines:
                return [("", text)]
            if self.user_lines[line_number]:
                return [("class:user-label", text[:3]), ("class:user-message", text[3:])]
            return [("class:user-message", text)]

        return line_fragments


class UserMessageWindow(Window):
    """Extend user-message backgrounds across actual rendered rows, including wraps."""

    def __init__(self, user_lines, **kwargs):
        super().__init__(**kwargs)
        self.user_lines = user_lines

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
        terminal_input=None,
        terminal_output=None,
    ):
        self.runtime = runtime
        self.sandbox = sandbox
        self.writeback = writeback
        self.thinking = thinking
        self.status = status
        self.history = ()
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
            lexer=ConversationLexer(self.user_lines),
        )
        self.chat.window = UserMessageWindow(
            self.user_lines,
            content=self.chat.control,
            style="class:chat",
            wrap_lines=True,
            right_margins=self.chat.window.right_margins,
        )
        self.editor = TextArea(
            height=3,
            multiline=True,
            wrap_lines=True,
            prompt="你> ",
            style="class:editor",
            completer=WordCompleter(
                ["/help", "/clear", "/exit", "/thinking", "/context", "/diff", "/apply", "/skills"]
            ),
        )
        keys = KeyBindings()

        @keys.add("enter")
        def submit(event):
            self.submit()

        @keys.add("escape", "enter")
        def newline(event):
            self.editor.buffer.insert_text("\n")

        @keys.add("s-tab")
        def thinking(event):
            if self.busy:
                self.phase = "任务运行中；结束后可切换思考设置"
            elif self.thinking:
                try:
                    self.thinking.cycle()
                    self.refresh_footer()
                except (ValueError, LLMError) as error:
                    self.append(f"\n设置未变更：{error}\n")

        @keys.add("c-c")
        def interrupt(event):
            if self.busy:
                self.cancelled.set()
                self.phase = "正在停止；等待当前网络读取或工具安全结束…"
            else:
                self.editor.text = ""
                self.phase = "输入已清空；Ctrl+D 退出"

        @keys.add("c-d")
        def exit_app(event):
            if self.busy:
                self.cancelled.set()
                self.phase = "正在停止；结束后再次 Ctrl+D 退出"
            elif not self.editor.text:
                self.app.exit()

        @keys.add("pageup")
        def up(event):
            self.follow = False
            self.chat.buffer.cursor_up(count=10)

        @keys.add("pagedown")
        def down(event):
            self.chat.buffer.cursor_down(count=10)
            self.follow = self.chat.buffer.cursor_position == len(self.chat.text)

        @keys.add("c-end")
        def end(event):
            self.follow = True
            self.chat.buffer.cursor_position = len(self.chat.text)

        self.app = Application(
            layout=Layout(
                HSplit(
                    [
                        Window(
                            FormattedTextControl(" CODING AGENT · 会话"),
                            height=1,
                            style="class:header",
                        ),
                        self.chat,
                        Window(
                            FormattedTextControl(lambda: " " + self.phase),
                            height=1,
                            style="class:status",
                        ),
                        Frame(self.editor, title="Enter 发送 · Alt+Enter 换行 · Tab 补全"),
                        Window(
                            FormattedTextControl(lambda: self.footer_text),
                            height=self.footer_height,
                            wrap_lines=True,
                            style="class:footer",
                        ),
                        Window(
                            FormattedTextControl(
                                " Shift+Tab 思考 · Ctrl+C 停止 · PgUp/PgDn 历史 · Ctrl+End 跟随"
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
                    "chat": "#dddddd",
                    "editor": "#ffffff",
                    "user-row": "bg:#20364d",
                    "user-message": "bg:#20364d #e5f2ff",
                    "user-label": "bg:#20364d #81c7ff bold",
                    "status": "#81c7ff",
                    "footer": "bg:#202630 #b9d7ed",
                    "hint": "#999999",
                }
            ),
        )
        self.refresh_footer()
        self.append("欢迎。输入任务开始；/help 查看命令。\n")

    def footer_height(self):
        width = max(1, self.app.output.get_size().columns)
        return sum(
            max(1, (get_cwidth(line) + width - 1) // width) for line in self.footer_text.split("\n")
        )

    def refresh_footer(self):
        self.footer_text = "\n".join(
            x
            for x in (
                self.thinking.describe() if self.thinking else "",
                self.status.describe() if self.status else "",
            )
            if x
        )
        self.app.invalidate()

    def append(self, text, *, user=False):
        if user:
            first_line = self.transcript.count("\n")
            for offset in range(len(text.splitlines())):
                self.user_lines[first_line + offset] = offset == 0
        self.transcript += text
        cursor = len(self.transcript) if self.follow else self.chat.buffer.cursor_position
        self.chat.buffer.set_document(Document(self.transcript, cursor), bypass_readonly=True)
        self.app.invalidate()

    def write(self, *values, sep=" ", end="\n", **kwargs):
        text = sep.join(str(v) for v in values) + end
        self.loop.call_soon_threadsafe(self.queue_text, text)

    def queue_text(self, text):
        self.pending_text.append(text)
        if self.flush_handle is None:
            self.flush_handle = self.loop.call_later(0.025, self.flush_text)

    def flush_text(self):
        if self.flush_handle:
            self.flush_handle.cancel()
            self.flush_handle = None
        if self.pending_text:
            text = "".join(self.pending_text)
            self.pending_text.clear()
            self.append(text)

    def submit(self):
        from .interactive import HELP, RESET_NOTICE, describe_skills

        task = self.editor.text.strip()
        if not task:
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
        if task == "/clear":
            self.history = ()
            if self.status:
                self.status.reset_context()
                self.refresh_footer()
            self.append(RESET_NOTICE + "\n")
            return
        if task == "/help":
            self.append(HELP + "\nAlt+Enter 换行；PgUp/PgDn 浏览历史；Ctrl+End 恢复跟随。\n")
            return
        if task == "/skills":
            self.append(describe_skills(self.runtime) + "\n")
            return
        if task.split()[0] == "/context" and self.status:
            try:
                self.append(self.status.context_command(task) + "\n")
                self.refresh_footer()
            except (ValueError, LLMError) as error:
                self.append(f"设置未变更：{error}\n")
            return
        if task.split()[0] == "/thinking" and self.thinking:
            try:
                self.thinking.command(task)
                self.refresh_footer()
                self.append(self.thinking.describe() + "\n")
            except (ValueError, LLMError) as error:
                self.append(f"设置未变更：{error}\n")
            return
        if task.startswith("/") and not (self.sandbox and task in {"/diff", "/apply"}):
            self.append("未知会话命令，请输入 /help。\n")
            return
        self.busy = True
        self.cancelled.clear()
        self.phase = "开始执行…"
        self.worker = asyncio.create_task(self.execute(task))

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise KeyboardInterrupt()

    def work(self, task):
        from .interactive import finish_writeback

        try:
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
            displayed = (
                result.stats
                and result.stats.model_calls
                and result.stats.model_calls[-1].first_display_seconds is not None
            )
            if result.text and not displayed:
                self.write(result.text)
            if result.status != "completed":
                self.write(f"任务尚未正常完成：{result.status}")
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
            if kind == "error":
                if self.sandbox:
                    self.sandbox.guard.needs_review = True
                self.history = ()
                if self.status:
                    self.status.reset_context()
                self.append("\n" + value + "\n" + RESET_NOTICE + "\n")
            elif kind == "result":
                self.history = () if value.status == "stopped" else value.history
                if value.status == "stopped" and self.status:
                    self.status.reset_context()
            self.phase = "就绪" if kind != "error" else "任务未完成，可重新输入"
        finally:
            self.busy = False
            self.refresh_footer()

    async def run_async(self):
        self.loop = asyncio.get_running_loop()
        previous = (
            self.runtime.on_event,
            self.runtime.on_model_event,
            self.runtime.check_cancelled,
        )
        live = LiveOutput(self.write)

        def event(name, stats):
            if previous[0]:
                previous[0](name, stats)
            # Snapshot mutable counters before passing them to the UI thread.
            footer = self.status.describe() if self.status else ""
            record = stats.model_calls[-1] if stats.model_calls else None
            phase = f"模型 #{record.step} · 等待响应…" if name == "model_start" else None
            if name == "tool_start":
                phase = f"工具 · {stats.tool_calls[-1].name} 执行中…"
            if name == "skill_loaded":
                self.write(f"[已加载技能：{stats.skill_loads[-1]['name']}]")
            self.loop.call_soon_threadsafe(self.progress, phase, footer)

        def model_event(kind, text, elapsed, record):
            if kind == "first_text":
                footer = self.status.describe() if self.status else ""
                self.loop.call_soon_threadsafe(
                    self.progress, f"模型 #{record.step} · 正在输出…", footer
                )
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
            self.runtime.on_event, self.runtime.on_model_event, self.runtime.check_cancelled = (
                previous
            )

    def progress(self, phase, footer):
        if phase and not self.cancelled.is_set():
            self.phase = phase
        self.footer_text = "\n".join(
            x for x in (self.thinking.describe() if self.thinking else "", footer) if x
        )
        self.app.invalidate()

    def run(self):
        asyncio.run(self.run_async())
        # Preserve a readable final transcript in the terminal scrollback/session log.
        print(self.transcript)
        print(self.footer_text)
        print("会话已结束。")
