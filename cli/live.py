"""Live model output and session-local thinking controls."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from llm import ConfigurationError
from llm.providers import get_provider
from llm.thinking import thinking_options


class SessionStatus:
    """Session totals use provider usage, including completed rounds of failed tasks."""

    def __init__(self, workspace, context_window=None):
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

    def reset_context(self):
        self.context_tokens = None
        self.context_note = "待请求"

    def describe_context(self):
        size = "未知" if self.context_tokens is None else f"≈{self.context_tokens:,}"
        if self.context_window is None:
            limit = "上限未设置（/context 设置）"
        elif self.context_tokens is None:
            limit = f"/ {self.context_window:,} tokens · 占用未知"
        else:
            percent = self.context_tokens / self.context_window * 100
            limit = f"/ {self.context_window:,} tokens · ≈{percent:.1f}%"
        return f"上下文 {size} {limit} · {self.context_note}"

    def context_command(self, text):
        args = text.split()[1:]
        if args:
            if len(args) != 1:
                raise ConfigurationError("用法：/context [模型上下文上限 token 数]")
            self.set_context_window(int(args[0]))
        return self.describe_context()

    def __call__(self, event, stats):
        if event == "model_start":
            self.context_note = "请求中，等待用量更新"
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
        self.context_tokens = (
            incoming + outgoing if incoming is not None and outgoing is not None else None
        )
        self.context_note = (
            "最近一轮输入＋输出估算" if self.context_tokens is not None else "接口未返回完整用量"
        )
        for field in self.totals:
            value = getattr(record.usage, field, None)
            if value is not None:
                self.totals[field] += value
                self.reported[field] += 1

    def describe(self):
        def total(field):
            if self.calls and not self.reported[field]:
                return "未知（接口未返回）"
            value = f"{self.totals[field]:,}"
            if self.reported[field] < self.calls:
                value += "（部分已知）"
            return value

        return (
            f"Session tokens · 输入 {total('input_tokens')} · "
            f"输出 {total('output_tokens')}\n{self.describe_context()}\n工作目录：{self.workspace}"
        )


class LiveOutput:
    def __init__(self, write=print):
        self.write = write
        self.last_record = None
        self.had_text = False

    def __call__(self, kind, text, elapsed, record):
        if record is not self.last_record:
            self.last_record = record
            self.had_text = False
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
            self.write(
                f"[模型 #{record.step}：首行数据 {render(data)} · 首字 {render(first)} · "
                f"生成 {render(tail)} · 显示延迟 {render(lag)} · 总计 {elapsed:.2f}s]",
                flush=True,
            )
        return None


class ThinkingControl:
    def __init__(self, runtime, args):
        self.runtime = runtime
        self.provider = get_provider(args.provider).name
        self.model = args.model
        self.limit = args.max_output_tokens
        self.base = json.loads(args.extra_json)
        self.current = {
            "mode": args.thinking,
            "effort": args.reasoning_effort,
            "budget": args.thinking_budget,
        }
        self.runtime.thinking_settings = deepcopy(self.current)

    def describe(self):
        c = self.current
        native = any(
            k in self.base
            for k in (
                "thinking",
                "reasoning",
                "reasoning_effort",
                "enable_thinking",
                "thinking_budget",
                "output_config",
            )
        ) or (
            isinstance(self.base.get("generationConfig"), dict)
            and "thinkingConfig" in self.base["generationConfig"]
        )
        if native:
            return "思考设置：使用 LLM_EXTRA_JSON 原生覆盖；请先移除原生思考配置再切换。"
        return (
            f"思考设置：{c['mode']} · 强度={c['effort'] or '服务端默认'} · "
            f"预算={c['budget'] or '服务端默认'} · 输出上限={self.limit} "
            "[Shift+Tab 切换；/thinking 查看]"
        )

    def set(self, mode="auto", effort=None, budget=None):
        options = thinking_options(
            self.provider,
            self.model,
            mode=mode,
            effort=effort,
            budget=budget,
            max_output_tokens=self.limit,
        )
        # Native overrides are opaque: don't silently erase or mix their thinking settings.
        fields = {
            "thinking",
            "reasoning",
            "reasoning_effort",
            "enable_thinking",
            "thinking_budget",
            "output_config",
        }
        if fields.intersection(self.base) or "thinkingConfig" in self.base.get(
            "generationConfig", {}
        ):
            raise ConfigurationError("请先移除 LLM_EXTRA_JSON 中的思考参数，再使用会话切换")
        merged = deepcopy(self.base)
        for key, value in options.items():
            if key == "generationConfig":
                merged.setdefault(key, {}).update(value)
            else:
                merged[key] = value
        # Validate the native payload before changing the live runtime.
        old = self.runtime.request_extra
        self.runtime.request_extra = merged
        try:
            from llm import Message

            request = self.runtime._request([Message("user", "validate")])
            adapter = getattr(self.runtime.llm, "adapter", None)
            if adapter:
                adapter.encode(request)
        except Exception:
            self.runtime.request_extra = old
            raise
        self.current = {"mode": mode, "effort": effort, "budget": budget}
        self.runtime.thinking_settings = deepcopy(self.current)

    def presets(self):
        settings = [("auto", None, None), ("disabled", None, None)]
        if self.provider in {"openai", "deepseek", "zhipu"}:
            settings += [("enabled", level, None) for level in ("low", "medium", "high", "max")]
        elif self.provider == "anthropic":
            settings += [("adaptive", level, None) for level in ("low", "medium", "high")]
        else:
            settings += [("enabled", None, None)]
        valid = []
        for mode, effort, budget in settings:
            try:
                thinking_options(
                    self.provider,
                    self.model,
                    mode=mode,
                    effort=effort,
                    budget=budget,
                    max_output_tokens=self.limit,
                )
                valid.append((mode, effort, budget))
            except ConfigurationError:
                continue
        return valid

    def cycle(self):
        presets = self.presets()
        current = tuple(self.current[k] for k in ("mode", "effort", "budget"))
        index = presets.index(current) + 1 if current in presets else 0
        self.set(*presets[index % len(presets)])

    def command(self, text):
        args = text.split()[1:]
        if not args:
            return
        if args == ["next"]:
            self.cycle()
            return
        mode = {"off": "disabled", "on": "enabled"}.get(args[0], args[0])
        effort, budget = None, None
        for value in args[1:]:
            if value.startswith("budget="):
                budget = int(value.split("=", 1)[1])
            elif effort is None:
                effort = value
            else:
                raise ConfigurationError(
                    "用法：/thinking auto|off|on|adaptive [强度] [budget=整数]"
                )
        self.set(mode, effort, budget)


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
                except (ValueError, ConfigurationError) as error:
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
