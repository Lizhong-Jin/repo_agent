"""Terminal descriptions and /context commands over reusable session state."""

from agent.session_state import SessionState
from llm import ConfigurationError

from .formatting import format_tokens


class SessionStatus(SessionState):
    def describe_context(self, *, compact=False):
        input_only = self.context_limit_kind == "input"
        tokens = self.context_input_tokens if input_only else self.context_tokens
        size = "未知" if tokens is None else f"≈{format_tokens(tokens)}"
        if self.context_window is None:
            limit = "上限未知（/context auto 重查或 /context 数值设置）"
        elif tokens is None:
            limit = f"/ {format_tokens(self.context_window)} tokens · 占用未知"
        else:
            percent = tokens / self.context_window * 100
            limit = f"/ {format_tokens(self.context_window)} tokens · ≈{percent:.1f}%"
        label = "输入上下文" if input_only else "上下文"
        if compact:
            if self.context_window is None:
                limit = "/ 上限未知"
            else:
                percent = "占用未知" if tokens is None else f"≈{tokens / self.context_window:.1%}"
                limit = f"/ {format_tokens(self.context_window)} · {percent}"
            pending = "（待更新）" if "待" in self.context_note and tokens is not None else ""
            estimate = "（本地粗估）" if "本地粗估" in self.context_note else ""
            return f"{label} {size} {limit}{estimate}{pending} · {self.describe_cache()}"
        note = self.context_note
        if input_only and note == "最近一轮输入＋输出估算":
            note = "最近一轮输入；服务端提供输入上限"
        elif input_only and tokens is not None and note == "接口未返回完整用量":
            note = "最近一轮输入；输出用量未知"
        source = f" · {self.context_limit_source}" if self.context_limit_source else ""
        return f"{label} {size} {limit} · {self.describe_cache()}{source} · {note}"

    def describe_cache(self):
        incoming, cached = self.context_input_tokens, self.context_cached_input_tokens
        if incoming is None or incoming <= 0 or cached is None or not 0 <= cached <= incoming:
            return "缓存命中 未知"
        return f"缓存命中 {cached / incoming:.1%}"

    def context_command(self, text):
        args = text.split()[1:]
        if args:
            if args == ["auto"]:
                self.auto_context_window(refresh=True)
            else:
                try:
                    if len(args) != 1:
                        raise ValueError
                    value = int(args[0])
                except ValueError:
                    raise ConfigurationError(
                        "用法：/context [auto|模型上下文上限 token 数]"
                    ) from None
                self.set_context_window(value)
                self.context_override = value
        return self.describe_context()

    def describe(self, *, compact=False):
        def total(field):
            if self.calls and not self.reported[field]:
                return "未知（接口未返回）"
            value = format_tokens(self.totals[field])
            if self.reported[field] < self.calls:
                value += "（部分已知）"
            return value

        usage = f"Session tokens · 输入 {total('input_tokens')} · 输出 {total('output_tokens')}"
        if compact:
            usage = usage.removeprefix("Session ")
            return f"{usage} · {self.describe_context(compact=True)}\n工作目录：{self.workspace}"
        return f"{usage}\n{self.describe_context()}\n工作目录：{self.workspace}"
