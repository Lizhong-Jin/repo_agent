"""CLI argument/persistence adapter and /thinking presentation."""

import json

from agent.thinking import ThinkingController
from llm import ConfigurationError
from llm.thinking_profiles import EFFORTS, history_default

from .formatting import format_tokens
from .thinking_store import load_preference, preference_path, save_preference


class FileThinkingPreferences:
    """Adapt the CLI preference store to the controller's persistence port."""

    load = staticmethod(load_preference)
    save = staticmethod(save_preference)


class ThinkingControl(ThinkingController):
    def __init__(self, runtime, args):
        super().__init__(
            runtime,
            provider=args.provider,
            model=args.model,
            base_url=getattr(args, "base_url", None),
            max_output_tokens=args.max_output_tokens,
            extra=json.loads(args.extra_json),
            profile=getattr(args, "thinking_profile", "{}"),
            mode=args.thinking,
            effort=args.reasoning_effort,
            budget=args.thinking_budget,
            history=getattr(args, "thinking_history", "auto"),
            preferences=FileThinkingPreferences()
            if getattr(args, "thinking_recall", True)
            else None,
        )

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
