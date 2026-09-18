"""Translate explicit thinking settings into provider request fields.

Model-specific availability is still checked by the provider. No model ID is rewritten.
"""

from typing import Any

from .errors import ConfigurationError
from .providers import get_provider


def thinking_options(
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
            if "gemini-3" in model or "gemini-2.5-pro" in model:
                raise ConfigurationError("This Gemini model cannot fully disable thinking")
            options = {"thinkingBudget": 0}
        elif effort or (mode == "enabled" and "gemini-3" in model and budget is None):
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
