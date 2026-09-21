"""User-wide display preference, independent of model effort and reasoning state."""

from .config import save_user_config

DISPLAY_MODES = ("collapsed", "expanded", "hidden")


def display_mode(value):
    if value not in DISPLAY_MODES:
        raise ValueError("思考显示必须为 collapsed、expanded 或 hidden")
    return value


class ThinkingDisplay:
    def __init__(self, mode="collapsed"):
        self.mode = display_mode(mode)

    def set(self, mode):
        mode = display_mode(mode)
        # Persist first so a failure leaves the live preference unchanged.
        save_user_config({"AGENT_THINKING_DISPLAY": mode})
        self.mode = mode
        return self.describe()

    def toggle(self):
        return self.set("collapsed" if self.mode == "expanded" else "expanded")

    def command(self, text):
        args = text.split()
        if args == ["/thinking", "display"]:
            return self.describe()
        if len(args) != 3 or args[:2] != ["/thinking", "display"]:
            raise ValueError("用法：/thinking display [collapsed|expanded|hidden]")
        return self.set(args[2])

    def describe(self):
        label = {"collapsed": "折叠", "expanded": "展开", "hidden": "隐藏"}[self.mode]
        return f"思考显示：{label} · Ctrl+T 展开/折叠"
