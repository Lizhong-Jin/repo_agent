"""Translate explicit thinking settings into provider request fields.

Known model constraints are checked locally; other availability is checked by the provider.
No model ID is rewritten.
"""

from typing import Any

from .errors import ConfigurationError
from .providers import get_provider
from .thinking_profiles import EFFORTS, thinking_profile


def normalize_settings(profile, *, mode="auto", effort=None, budget=None, history="auto"):
    if history not in ("auto", "on", "off"):
        raise ConfigurationError("历史思考保留必须为 auto、on 或 off")
    if history != "auto" and not profile.history:
        raise ConfigurationError("当前模型没有适配历史思考保留开关")
    if effort is not None and effort not in EFFORTS:
        raise ConfigurationError("无效思考强度；使用 /thinking list 查看可用档位")
    effort = profile.aliases.get(effort, effort)
    if profile.known:
        if (
            mode in ("enabled", "adaptive")
            and effort is None
            and budget is None
            and profile.efforts
        ):
            effort = (
                profile.default_effort
                if profile.default_effort in profile.efforts
                else "medium"
                if "medium" in profile.efforts
                else profile.efforts[-1]
            )
        if mode == "enabled" and "enabled" not in profile.modes and "adaptive" in profile.modes:
            mode = "adaptive"
        if mode not in profile.modes:
            raise ConfigurationError(
                "当前模型不能关闭思考" if mode == "disabled" else "当前模型不支持此思考模式"
            )
        if effort is not None and effort not in profile.efforts:
            raise ConfigurationError("当前模型不支持此强度；使用 /thinking list 查看可用档位")
        if budget is not None:
            if profile.budget_min is None:
                raise ConfigurationError("当前模型不支持固定思考预算")
            if (
                type(budget) is not int
                or budget < profile.budget_min
                or (profile.budget_max is not None and budget > profile.budget_max)
            ):
                raise ConfigurationError("思考预算超出模型范围；使用 /thinking list 查看")
    return {"mode": mode, "effort": effort, "budget": budget, "history": history}


def thinking_options(
    provider,
    model,
    *,
    mode="auto",
    effort=None,
    budget=None,
    max_output_tokens=4096,
    history="auto",
    base_url=None,
    profile=None,
):
    capability = thinking_profile(provider, model, base_url=base_url, override=profile)
    settings = normalize_settings(
        capability, mode=mode, effort=effort, budget=budget, history=history
    )
    settings.pop("history")
    result = _native_options(provider, model, max_output_tokens=max_output_tokens, **settings)
    if history != "auto":
        result.setdefault("thinking", {"type": "enabled"})["clear_thinking"] = history == "off"
    return result


def _native_options(
    provider: str,
    model: str,
    *,
    mode: str = "auto",
    effort: str | None = None,
    budget: int | None = None,
    max_output_tokens: int = 4096,
) -> dict[str, Any]:
    """auto omits the switch; adaptive is an explicit Claude thinking mode."""
    name = get_provider(provider).name
    if mode not in {"auto", "enabled", "disabled", "adaptive"}:
        raise ConfigurationError("LLM_THINKING must be auto, enabled, disabled or adaptive")
    if effort is not None and effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigurationError(
            "LLM_REASONING_EFFORT must be minimal, low, medium, high, xhigh or max"
        )
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ConfigurationError("LLM_THINKING_BUDGET must be a positive integer")
    if mode == "disabled" and (effort is not None or budget is not None):
        raise ConfigurationError("Disabled thinking cannot be combined with effort or budget")
    if mode == "adaptive" and name != "anthropic":
        raise ConfigurationError("adaptive thinking is only mapped for Claude")
    if mode == "auto" and effort is None and budget is None:
        return {}

    if name == "openai":
        if budget is not None:
            raise ConfigurationError("OpenAI uses LLM_REASONING_EFFORT, not LLM_THINKING_BUDGET")
        return {"reasoning": {"effort": "none" if mode == "disabled" else effort or "medium"}}

    if name == "anthropic":
        result: dict[str, Any] = {}
        if mode == "enabled" or budget is not None:
            if mode == "adaptive":
                raise ConfigurationError("Adaptive thinking cannot be combined with a fixed budget")
            if budget is None or not 1024 <= budget < max_output_tokens:
                raise ConfigurationError(
                    "Claude enabled thinking requires 1024 <= LLM_THINKING_BUDGET "
                    "< AGENT_MAX_OUTPUT_TOKENS; use adaptive for models requiring adaptive thinking"
                )
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}
        elif mode != "auto":
            result["thinking"] = {"type": mode}
        if effort:
            if effort not in {"low", "medium", "high", "max"}:
                raise ConfigurationError("Claude effort must be low, medium, high or max")
            result["output_config"] = {"effort": effort}
        return result

    if name == "gemini":
        if effort and budget is not None:
            raise ConfigurationError("Gemini accepts a thinking level or budget, not both")
        if mode == "disabled":
            options = {"thinkingBudget": 0}
        elif effort:
            if effort and effort not in {"minimal", "low", "medium", "high"}:
                raise ConfigurationError(
                    "Gemini thinking level must be minimal, low, medium or high"
                )
            options = {"thinkingLevel": effort or "high"}
        else:
            options = {"thinkingBudget": budget if budget is not None else -1}
        return {"generationConfig": {"thinkingConfig": options}}

    if name == "qwen":
        if effort:
            raise ConfigurationError("Qwen uses LLM_THINKING_BUDGET, not LLM_REASONING_EFFORT")
        result = {}
        if mode != "auto":
            result["enable_thinking"] = mode == "enabled"
        if budget is not None:
            result["thinking_budget"] = budget
        return result

    if name in {"deepseek", "moonshot", "zhipu", "doubao"}:
        if budget is not None:
            raise ConfigurationError("This provider has no mapped LLM_THINKING_BUDGET parameter")
        if effort and name not in {"deepseek", "zhipu"}:
            raise ConfigurationError("This provider has no mapped LLM_REASONING_EFFORT parameter")
        result = {}
        if mode != "auto":
            result["thinking"] = {"type": mode}
        if effort:
            result["reasoning_effort"] = effort
        return result

    raise ConfigurationError(
        "This provider has no mapped thinking controls; keep LLM_THINKING=auto "
        "and leave effort/budget empty"
    )
