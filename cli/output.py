"""Stream model text, thinking and usage to a print-compatible output sink."""

from time import perf_counter

from agent.transcript import display_text

from .formatting import format_tokens
from .thinking_display import ThinkingDisplay


class LiveOutput:
    def __init__(self, write=print, *, display=None, write_meta=None):
        self.write = write
        self.write_meta = write_meta or write
        self.display = display or ThinkingDisplay()
        self.thinking_visible = False
        self.last_record = None
        self.had_text = False

    def __call__(self, kind, text, elapsed, record):
        if record is not self.last_record:
            self.last_record = record
            self.had_text = False
        if kind == "thinking_start":
            self.thinking_visible = self.display.mode == "expanded"
            if self.display.mode != "hidden":
                self.write_meta(f"\n[思考 · 模型 #{record.step}]", flush=True)
        elif kind == "thinking_delta" and self.thinking_visible:
            self.write(display_text(text), end="", flush=True)
        elif kind == "thinking_end" and self.thinking_visible:
            self.write(flush=True)
            self.thinking_visible = False
        elif kind == "usage":
            usage = record.usage
            if usage:

                def count(value):
                    return format_tokens(value)

                self.write_meta(
                    f"[用量：输入 {count(usage.input_tokens)} · 输出 {count(usage.output_tokens)}"
                    f" · 其中思考 {count(usage.reasoning_tokens)}]",
                    flush=True,
                )
        elif kind == "thinking_unavailable" and self.display.mode != "hidden":
            self.write_meta("[接口未提供可显示的思考内容]", flush=True)
        if kind == "text":
            started = perf_counter()
            self.write(text, end="", flush=True)
            self.had_text = True
            return elapsed + perf_counter() - started
        if kind == "end":
            if self.had_text:
                self.write(flush=True)
            first = record.first_text_seconds
            data = record.first_data_seconds
            display = record.first_display_seconds

            def render(value):
                return "未收到" if value is None else f"{value:.2f}s"

            tail = None if first is None else elapsed - first
            lag = None if first is None or display is None else max(0, display - first)
            self.write_meta(
                f"[模型 #{record.step}：首行数据 {render(data)} · "
                f"首段思考 {render(record.first_thinking_seconds)} · 首字 {render(first)} · "
                f"生成 {render(tail)} · 显示延迟 {render(lag)} · 总计 {elapsed:.2f}s]",
                flush=True,
            )
        return None
