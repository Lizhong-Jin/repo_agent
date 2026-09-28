"""Transcript styling and scroll behavior for prompt_toolkit widgets."""

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.screen import WritePosition
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType


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
