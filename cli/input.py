"""Line-mode terminal input with optional thinking shortcuts."""

import sys

from llm import ConfigurationError


class SessionInput:
    def __init__(self, control, *, status=None, terminal_input=None, terminal_output=None):
        self.status = status
        self.control = control
        self.session = None
        self.notice = ""
        if control is not None and (
            terminal_input is not None or (sys.stdin.isatty() and sys.stdout.isatty())
        ):
            from prompt_toolkit import PromptSession
            from prompt_toolkit.key_binding import KeyBindings

            bindings = KeyBindings()

            @bindings.add("s-tab")
            def switch(event):
                try:
                    control.cycle()
                    self.notice = "（下一次请求生效）"
                except (ValueError, OSError, ConfigurationError) as error:
                    self.notice = str(error)
                event.app.invalidate()

            self.session = PromptSession(
                input=terminal_input,
                output=terminal_output,
                key_bindings=bindings,
                bottom_toolbar=self.toolbar,
            )

    def toolbar(self):
        thinking = self.control.describe() + self.notice if self.control else ""
        return "\n".join(
            part for part in (thinking, self.status.describe() if self.status else "") if part
        )

    def read(self, prompt):
        return self.session.prompt(prompt) if self.session else input(prompt)
