"""Live model output and session-local thinking controls."""

import json
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from llm import ConfigurationError
from llm.providers import get_provider
from llm.thinking import normalize_settings, thinking_options
from llm.thinking_profiles import EFFORTS, history_default, parse_profile, thinking_profile

from .formatting import format_tokens
from .settings import native_thinking
from .thinking_display import ThinkingDisplay
from .thinking_store import load_preference, preference_path, save_preference
from .transcript import display_text


class SessionStatus:
    """Session totals use provider usage, including completed rounds of failed tasks."""

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
            self.context_limit_source = "服务端自动获取"

    def reset_context(self):
        self.context_tokens = None
        self.context_input_tokens = None
        self.context_note = "待请求"

    def initialize_context(self, runtime, history=()):
        """Fill the startup gap without turning an estimate into session usage."""
        known = (
            self.context_input_tokens
            if self.context_limit_kind == "input" else self.context_tokens
        )
        if known is not None and "本地粗估" not in self.context_note:
            return
        self.context_tokens = runtime.estimate_context_tokens(history)
        self.context_input_tokens = self.context_tokens
        self.context_note = "本地粗估；不含草稿，服务端用量返回后更新"

    def reset_session(self):
        self.reset_context()
        self.calls = 0
        self.totals = {"input_tokens": 0, "output_tokens": 0}
        self.reported = {"input_tokens": 0, "output_tokens": 0}
        self._seen = set()

    def session_state(self):
        return {name: deepcopy(getattr(self, name)) for name in (
            "calls", "totals", "reported", "context_tokens", "context_input_tokens",
            "context_note", "context_window", "context_limit_source", "context_override",
        )}

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
            self.context_note = display_text(data["context_note"])
        else:
            self.reset_context()
        if restore_window and override is not None:
            self.set_context_window(override)
            self.context_override = override

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
            return f"{label} {size} {limit}{estimate}{pending}"
        note = self.context_note
        if input_only and note == "最近一轮输入＋输出估算":
            note = "最近一轮输入；服务端提供输入上限"
        elif input_only and tokens is not None and note == "接口未返回完整用量":
            note = "最近一轮输入；输出用量未知"
        source = f" · {self.context_limit_source}" if self.context_limit_source else ""
        return f"{label} {size} {limit}{source} · {note}"

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

    def __call__(self, event, stats):
        if event == "model_start":
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
        self.context_input_tokens = incoming
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


class ThinkingControl:
    def __init__(self, runtime, args):
        self.runtime = runtime
        self.provider = get_provider(args.provider).name
        self.model = args.model
        self.base_url = getattr(args, "base_url", None)
        self.limit = args.max_output_tokens
        self.base = json.loads(args.extra_json)
        self.override = parse_profile(getattr(args, "thinking_profile", "{}"))
        self.recall = getattr(args, "thinking_recall", True)
        self.current = normalize_settings(
            self.profile,
            mode=args.thinking,
            effort=args.reasoning_effort,
            budget=args.thinking_budget,
            history=getattr(args, "thinking_history", "auto"),
        )
        self.runtime.thinking_settings = deepcopy(self.current)

    @property
    def profile(self):
        return thinking_profile(
            self.provider, self.model, base_url=self.base_url, override=self.override
        )

    def native_override(self):
        return native_thinking(self.base)

    def describe(self, *, compact=False):
        if self.native_override():
            if compact:
                return "思考 原生配置"
            return "思考设置：使用 LLM_EXTRA_JSON 原生覆盖；请先移除原生思考配置再切换。"
        c, p = self.current, self.profile
        effort = c["effort"] or (
            f"{p.default_effort}（服务端默认）" if p.default_effort else "服务端默认"
        )
        if compact:
            mode = c["mode"]
            level = c["effort"] or p.default_effort
            label = mode + (f"/{level}" if level and mode != "disabled" else "")
            budget = f" · 预算 {format_tokens(c['budget'])}" if c["budget"] else ""
            return f"思考 {label}{budget} · 输出上限 {format_tokens(self.limit)}"
        history = {"on": "保留", "off": "清除跨轮思考"}.get(
            c["history"], "默认：" + history_default(self.provider, self.base_url)
        )
        history_text = f" · 历史思考={history}" if p.history else ""
        return (
            f"思考设置：{c['mode']} · 强度={effort}"
            f" · 预算={format_tokens(c['budget'], unknown='服务端默认')}"
            f"{history_text} · 输出上限={format_tokens(self.limit)} · {p.source} "
            "[Shift+Tab 切换；/thinking list 查看；"
            f"{'按模型记忆' if self.recall else '仅当前会话'}]"
        )

    def details(self):
        p = self.profile
        options = []
        for mode, effort, budget in self.presets():
            label = {"disabled": "off", "enabled": "on"}.get(mode, mode)
            options.append(
                " ".join(
                    str(v) for v in (label, effort, f"budget={budget}" if budget else None) if v
                )
            )
        lines = [self.describe(), "可切换档位：" + " → ".join(options)]
        if p.aliases:
            lines.append("强度别名：" + "，".join(f"{a} → {b}" for a, b in p.aliases.items()))
        if p.budget_min is not None:
            ceiling = format_tokens(p.budget_max) if p.budget_max is not None else "由模型决定"
            lines.append(
                f"预算范围：{format_tokens(p.budget_min)}～{ceiling}；/thinking budget 2048"
            )
            if self.provider == "anthropic":
                lines.append(f"手动预算还必须小于本次输出上限 {format_tokens(self.limit)}。")
        if p.history:
            lines.append("/thinking history on|off|auto：控制服务端历史思考保留；不删除本地历史。")
        if not p.known:
            lines.append("未知模型仅自动提供 auto；可显式设置或用 LLM_THINKING_PROFILE 声明能力。")
        lines.append(f"偏好文件：{preference_path()}（{'已启用' if self.recall else '已禁用'}）")
        lines.append("/thinking reset：恢复默认并忘记此模型偏好；/thinking low|high 等直接选档。")
        return "\n".join(lines)

    def _prepare(self, mode="auto", effort=None, budget=None, history="auto"):
        current = normalize_settings(
            self.profile, mode=mode, effort=effort, budget=budget, history=history
        )
        options = thinking_options(
            self.provider,
            self.model,
            **current,
            max_output_tokens=self.limit,
            base_url=self.base_url,
            profile=self.override,
        )
        if self.native_override():
            raise ConfigurationError("请先移除 LLM_EXTRA_JSON 中的思考参数，再使用会话切换")
        merged = deepcopy(self.base)
        for key, value in options.items():
            if key == "generationConfig":
                merged.setdefault(key, {}).update(value)
            else:
                merged[key] = value
        return current, merged

    def set(self, mode="auto", effort=None, budget=None, history=None, *, forget=False):
        current, merged = self._prepare(
            mode,
            effort,
            budget,
            self.current["history"] if history is None else history,
        )
        # Build and validate without mutating the live runtime, even if persistence fails.
        from dataclasses import replace

        from llm import Message

        request = replace(self.runtime._request([Message("user", "validate")]), extra=merged)
        adapter = getattr(self.runtime.llm, "adapter", None)
        if adapter:
            adapter.encode(request)
        if self.recall:
            save_preference(
                self.provider, self.model, self.base_url, current, self.override, forget=forget
            )
        self.runtime.request_extra = merged
        self.current = current
        self.runtime.thinking_settings = deepcopy(current)

    def prepare_model(self, selection, client):
        from copy import copy
        from dataclasses import replace

        from llm import Message

        candidate = copy(self)
        candidate.provider = get_provider(selection.provider).name
        candidate.model, candidate.base_url = selection.model, selection.base_url
        candidate.base, candidate.override = {}, {}
        settings = {"mode": "auto", "effort": None, "budget": None, "history": "auto"}
        saved = (
            load_preference(candidate.provider, candidate.model, candidate.base_url)
            if self.recall
            else None
        )
        if saved:
            settings, candidate.override = saved["settings"], saved["profile"]
        candidate.current, merged = candidate._prepare(**settings)
        request = replace(self.runtime._request([Message("user", "validate")]), extra=merged)
        client.adapter.encode(request)
        return candidate, merged

    def adopt(self, candidate):
        for key in ("provider", "model", "base_url", "base", "override", "current"):
            setattr(self, key, getattr(candidate, key))
        self.runtime.thinking_settings = deepcopy(self.current)

    def presets(self):
        p = self.profile
        settings = [("auto", None, None)]
        if not p.known:
            return settings
        if "disabled" in p.modes:
            settings.append(("disabled", None, None))
        if p.efforts:
            settings.extend((p.effort_mode, level, None) for level in p.efforts)
        elif "enabled" in p.modes:
            if p.budget_min is not None:
                budgets = sorted({p.budget_min, 2048, 8192, 16384})
                settings.extend(("enabled", None, b) for b in budgets if b < self.limit)
            else:
                settings.append(("enabled", None, None))
        valid = []
        for mode, effort, budget in settings:
            try:
                thinking_options(
                    self.provider,
                    self.model,
                    mode=mode,
                    effort=effort,
                    budget=budget,
                    history=self.current["history"],
                    max_output_tokens=self.limit,
                    base_url=self.base_url,
                    profile=self.override,
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
        if not args or args in (["list"], ["help"]):
            return self.details()
        if args == ["next"]:
            self.cycle()
        elif args == ["reset"]:
            self.set(history="auto", forget=True)
        elif len(args) == 2 and args[0] == "history":
            self.set(self.current["mode"], self.current["effort"], self.current["budget"], args[1])
        elif len(args) == 2 and args[0] == "budget":
            self.set("enabled", budget=int(args[1]))
        else:
            mode = {"off": "disabled", "on": "enabled"}.get(args[0], args[0])
            effort, budget = None, None
            if mode in EFFORTS:
                effort, mode = mode, self.profile.effort_mode
            for value in args[1:]:
                if value.startswith("budget=") and budget is None:
                    budget = int(value.split("=", 1)[1])
                elif effort is None and not value.startswith("budget="):
                    effort = value
                else:
                    raise ConfigurationError(
                        "用法：/thinking list|reset|档位|on 强度|budget 整数|history on/off/auto"
                    )
            self.set(mode, effort, budget)
        return self.describe()


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
