"""Construct terminal widgets and layout; the application supplies UI actions."""

from prompt_toolkit import Application
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer, DynamicContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

from ..shortcuts import shortcut_label
from .bindings import create_bindings
from .widgets import ConversationLexer, UserMessageWindow


def build_layout(ui, *, terminal_input=None, terminal_output=None):
    ui.chat = TextArea(
        read_only=True,
        scrollbar=True,
        wrap_lines=True,
        focusable=True,
        style="class:chat",
        lexer=ConversationLexer(ui.user_lines, ui.thinking_lines, ui.blocks.agent_lines),
    )
    ui.chat.window = UserMessageWindow(
        ui.user_lines,
        following=lambda: ui.follow,
        scroll_history=ui.scroll_history,
        content=ui.chat.control,
        style="class:chat",
        wrap_lines=True,
        right_margins=ui.chat.window.right_margins,
    )
    ui.editor = TextArea(
        height=3,
        multiline=True,
        wrap_lines=True,
        prompt=lambda: (
            {"provider": "供应商> ", "model": "搜索> ", "key": "API Key> "}[ui.model_wizard.stage]
            if ui.model_wizard
            else "你> "
        ),
        password=Condition(lambda: ui.model_wizard is not None and ui.model_wizard.secret),
        style="class:editor",
        completer=WordCompleter(
            [
                "/help",
                "/clear",
                "/compact",
                "/new",
                "/rename",
                "/sessions",
                "/switch",
                "/logs",
                "/report",
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

    conversation_body = HSplit(
        [
            Window(
                FormattedTextControl(ui.header_text),
                height=1,
                style="class:header",
            ),
            ui.chat,
            ConditionalContainer(
                DynamicContainer(lambda: ui.model_picker.window if ui.model_picker else Window()),
                filter=Condition(
                    lambda: ui.model_wizard is not None and ui.model_wizard.stage == "model"
                ),
            ),
            Window(
                FormattedTextControl(ui.phase_text),
                height=1,
                style="class:status",
            ),
            Frame(
                ui.editor,
                title=lambda: (
                    "模型设置 · Enter 确认 · Ctrl+C 取消"
                    if ui.model_wizard
                    else f"{shortcut_label('Enter')} 发送 · "
                    f"{shortcut_label('Alt+Enter')} 换行 · Tab 补全"
                ),
            ),
            Window(
                FormattedTextControl(lambda: ui.footer_text),
                height=ui.footer_height,
                wrap_lines=True,
                style="class:footer",
            ),
            Window(
                FormattedTextControl(
                    f" {shortcut_label('F2')} 改名 · {shortcut_label('F3')} 报告 · "
                    "Shift+Tab 强度 · "
                    "Ctrl+T 思考显示 · Ctrl+C 停止 · "
                    f"滚轮/{shortcut_label('PgUp')}/{shortcut_label('PgDn')} 历史"
                ),
                height=1,
                style="class:hint",
            ),
        ]
    )

    ui.app = Application(
        layout=Layout(
            DynamicContainer(
                lambda: ui.report_page.container if ui.report_page else conversation_body
            ),
            focused_element=ui.editor,
        ),
        key_bindings=create_bindings(ui),
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
