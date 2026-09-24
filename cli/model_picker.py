"""Searchable, bounded model menu shared by startup and the session TUI."""

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

from llm.model_catalog import supported_models


def thinking_label(value):
    if value.startswith("budget:"):
        budget = value.removeprefix("budget:")
        return "开启（预算上限未知）" if budget == "unknown" else f"预算 {int(budget):,}"
    return {"disabled": "关闭", "enabled": "开启", "auto": "接口默认"}.get(value, value)


class ModelPicker:
    page_size = 8

    def __init__(self, provider, current=""):
        self.models = supported_models(provider)
        self.query = ""
        self.matches = self.models
        self.index = next((i for i, m in enumerate(self.matches) if m.id.casefold() == current.casefold()), 0)
        self.window = Window(FormattedTextControl(self.fragments), height=12, wrap_lines=False)

    @property
    def selected(self):
        return self.matches[self.index] if self.matches else None

    def search(self, query):
        query = query.strip().casefold()
        if query == self.query:
            return
        self.query = query
        terms = query.split()
        self.matches = tuple(
            m
            for m in self.models
            if all(term in f"{m.provider} {m.id}".casefold() for term in terms)
        )
        self.index = next((i for i, m in enumerate(self.matches) if m.id.casefold() == query), 0)

    def move(self, amount):
        self.index = max(0, min(self.index + amount, len(self.matches) - 1))

    def value(self):
        if self.selected is None:
            raise ValueError("没有匹配的已支持模型，请修改搜索词")
        return self.selected.id

    def mouse_handler(self, index):
        def handle(event):
            if event.event_type == MouseEventType.SCROLL_UP:
                self.move(-1)
            elif event.event_type == MouseEventType.SCROLL_DOWN:
                self.move(1)
            elif event.event_type == MouseEventType.MOUSE_UP:
                self.index = index
            else:
                return NotImplemented

        return handle

    def fragments(self):
        result = [
            (
                "",
                f"模型列表 · {len(self.matches)}/{len(self.models)} · "
                "↑↓ / 滚轮选择 · PgUp/PgDn 翻页 · Enter 确认\n",
            )
        ]
        if not self.matches:
            return result + [("", "没有匹配的已支持模型\n")]
        offset = max(0, min(self.index - self.page_size // 2, len(self.matches) - self.page_size))
        for i in range(offset, min(offset + self.page_size, len(self.matches))):
            current = i == self.index
            result.append(
                (
                    "reverse" if current else "",
                    f"{'›' if current else ' '} {self.matches[i].id}\n",
                    self.mouse_handler(i),
                )
            )
        model = self.selected
        context = f"{model.context_window:,}" if model.context_window is not None else "未知"
        output = f"{model.max_output_tokens:,}" if model.max_output_tokens is not None else "未知"
        kind = "输入上限" if model.context_kind == "input" else "上下文"
        result.append(
            (
                "",
                f"{kind} {context} · 最大输出 {output}\n"
                f"思考范围 {thinking_label(model.min_thinking)} → "
                f"{thinking_label(model.max_thinking)}",
            )
        )
        return result


def pick_model(provider, current="", *, terminal_input=None, terminal_output=None):
    picker = ModelPicker(provider, current)
    search = TextArea(height=1, multiline=False, prompt="搜索> ")
    search.buffer.on_text_changed += lambda _: picker.search(search.text)
    keys = KeyBindings()

    @keys.add("up")
    def up(event):
        picker.move(-1)

    @keys.add("down")
    def down(event):
        picker.move(1)

    @keys.add("pageup")
    def pageup(event):
        picker.move(-picker.page_size)

    @keys.add("pagedown")
    def pagedown(event):
        picker.move(picker.page_size)

    @keys.add("enter")
    def accept(event):
        if picker.selected is not None:
            event.app.exit(result=picker.value())

    @keys.add("c-c")
    @keys.add("c-d")
    @keys.add("escape")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    app = Application(
        layout=Layout(HSplit([picker.window, search]), focused_element=search),
        key_bindings=keys,
        full_screen=False,
        mouse_support=True,
        input=terminal_input,
        output=terminal_output,
        style=Style.from_dict({}),
    )
    return app.run()
