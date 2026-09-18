"""Per-request timings and public text deltas; never expose hidden reasoning."""

from time import perf_counter


class RequestEvents:
    def __init__(self, callback=None):
        self.callback = callback
        self.started = perf_counter()
        self.first_data = False
        self.first_text = False

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
        if not self.first_text:
            self.first_text = True
            self.emit("first_text")
        self.emit("text", text)
