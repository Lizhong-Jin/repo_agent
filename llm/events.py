"""Per-request timings, answer deltas and provider-returned thinking text only."""

from time import perf_counter


class RequestEvents:
    def __init__(self, callback=None):
        self.callback = callback
        self.started = perf_counter()
        self.first_data = False
        self.first_text = False
        self.first_thinking = False
        self.thinking_active = False

    def thinking(self, text):
        if not text:
            return
        if not self.first_thinking:
            self.first_thinking = True
            self.emit("first_thinking")
        if not self.thinking_active:
            self.thinking_active = True
            self.emit("thinking_start")
        self.emit("thinking_delta", text)

    def end_thinking(self):
        if self.thinking_active:
            self.thinking_active = False
            self.emit("thinking_end")

    def emit(self, kind, text=""):
        if self.callback:
            self.callback(kind, text, perf_counter() - self.started)

    def data(self):
        if not self.first_data:
            self.first_data = True
            self.emit("first_data")

    def text(self, text):
        if not text:
            return
        self.end_thinking()
        if not self.first_text:
            self.first_text = True
            self.emit("first_text")
        self.emit("text", text)
