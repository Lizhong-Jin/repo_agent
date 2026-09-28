"""Session usage and context accounting, independent of terminal presentation."""

from copy import deepcopy
from pathlib import Path

from llm import ConfigurationError

from .transcript import display_text


class SessionState:
    """Count completed model rounds, including rounds in failed tasks and compaction."""

    def __init__(self, workspace, context_window=None):
        self._context_resolver = None
        self.set_context_window(context_window)
        self.reset_context()
        self.workspace = str(Path(workspace).resolve())
        self.calls = 0
        self.totals = {"input_tokens": 0, "output_tokens": 0}
        self.reported = {"input_tokens": 0, "output_tokens": 0}
        self._seen = set()

    def set_context_window(self, value):
        if value is not None and (type(value) is not int or value <= 0):
            raise ConfigurationError("模型上下文上限必须是正整数 token 数")
        self.context_window = value
        self.context_override = None
        self.context_limit_kind = "context"
        self.context_limit_source = "手动设置" if value is not None else ""

    def bind_context_model(self, client, *, reset=False):
        self._context_resolver = getattr(client, "get_context_limit", None)
        if reset:
            self.set_context_window(None)
        if self.context_window is None:
            self.auto_context_window()

    def auto_context_window(self, *, refresh=False):
        limit = self._context_resolver(refresh=refresh) if self._context_resolver else None
        self.set_context_window(None)
        self.context_limit_source = "自动获取未返回上限"
        if limit is not None:
            self.context_window = limit.tokens
            self.context_limit_kind = limit.kind
            self.context_limit_source = limit.source

    def reset_context(self):
        self.context_tokens = None
        self.context_input_tokens = None
        self.context_cached_input_tokens = None
        self.context_note = "待请求"

    def initialize_context(self, runtime, history=()):
        """Fill the startup gap without turning an estimate into session usage."""
        known = (
            self.context_input_tokens if self.context_limit_kind == "input" else self.context_tokens
        )
        if known is not None and "本地粗估" not in self.context_note:
            return
        self.context_tokens = runtime.estimate_context_tokens(history)
        self.context_input_tokens = self.context_tokens
        self.context_cached_input_tokens = None
        self.context_note = "本地粗估；不含草稿，服务端用量返回后更新"

    def reset_session(self):
        self.reset_context()
        self.calls = 0
        self.totals = {"input_tokens": 0, "output_tokens": 0}
        self.reported = {"input_tokens": 0, "output_tokens": 0}
        self._seen = set()

    def session_state(self):
        return {
            name: deepcopy(getattr(self, name))
            for name in (
                "calls",
                "totals",
                "reported",
                "context_tokens",
                "context_input_tokens",
                "context_cached_input_tokens",
                "context_note",
                "context_window",
                "context_limit_source",
                "context_override",
            )
        }

    def restore_session(self, data, *, context=True, restore_window=True):
        def counter(value):
            return type(value) is int and value >= 0

        try:
            if not counter(data["calls"]):
                raise ValueError
            for field in ("totals", "reported"):
                if set(data[field]) != {"input_tokens", "output_tokens"}:
                    raise ValueError
                if not all(counter(value) for value in data[field].values()):
                    raise ValueError
            if any(value > data["calls"] for value in data["reported"].values()):
                raise ValueError
            for field in ("context_tokens", "context_input_tokens"):
                if data[field] is not None and not counter(data[field]):
                    raise ValueError
            cached = data.get("context_cached_input_tokens")
            if cached is not None and not counter(cached):
                raise ValueError
            window = data["context_window"]
            if window is not None and (not counter(window) or window == 0):
                raise ValueError
            override = data.get("context_override")
            if override is not None and (not counter(override) or override == 0):
                raise ValueError
            if not isinstance(data["context_note"], str):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("会话用量记录无效；可使用 --new-session 启动新会话") from None
        self.calls = data["calls"]
        self.totals, self.reported = dict(data["totals"]), dict(data["reported"])
        self._seen.clear()
        if context:
            self.context_tokens = data["context_tokens"]
            self.context_input_tokens = data["context_input_tokens"]
            self.context_cached_input_tokens = cached
            self.context_note = display_text(data["context_note"])
        else:
            self.reset_context()
        if restore_window and override is not None:
            self.set_context_window(override)
            self.context_override = override

    def __call__(self, event, stats):
        compact_call = (
            bool(stats.model_calls)
            and getattr(stats.model_calls[-1], "purpose", "task") == "compaction"
        )
        if event == "model_start" and not compact_call:
            prefix = "本地粗估；" if "本地粗估" in self.context_note else ""
            self.context_note = prefix + "请求中，等待用量更新"
        elif event == "tool_end":
            self.context_note = "工具结果待下轮请求计数"
        if event != "model_end":
            return
        record = stats.model_calls[-1]
        key = (stats.task_id, record.step)
        if key in self._seen:
            return
        self._seen.add(key)
        self.calls += 1
        incoming = getattr(record.usage, "input_tokens", None)
        outgoing = getattr(record.usage, "output_tokens", None)
        if not compact_call:
            self.context_input_tokens = incoming
            self.context_cached_input_tokens = getattr(record.usage, "cached_input_tokens", None)
            self.context_tokens = (
                incoming + outgoing if incoming is not None and outgoing is not None else None
            )
            self.context_note = (
                "最近一轮输入＋输出估算"
                if self.context_tokens is not None
                else "接口未返回完整用量"
            )
        for field in self.totals:
            value = getattr(record.usage, field, None)
            if value is not None:
                self.totals[field] += value
                self.reported[field] += 1
