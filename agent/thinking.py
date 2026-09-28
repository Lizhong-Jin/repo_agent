"""Validate and apply thinking settings without CLI arguments or filesystem policy."""

from copy import deepcopy
from typing import Protocol

from llm import ConfigurationError
from llm.providers import get_provider
from llm.thinking import native_thinking, normalize_settings, thinking_options
from llm.thinking_profiles import parse_profile, thinking_profile


class ThinkingPreferences(Protocol):
    """Optional persistence port; implementations must save atomically or raise."""

    def load(self, provider, model, base_url): ...

    def save(self, provider, model, base_url, settings, profile, *, forget=False): ...


class ThinkingController:
    def __init__(
        self,
        runtime,
        *,
        provider,
        model,
        max_output_tokens,
        base_url=None,
        extra=None,
        profile=None,
        mode="auto",
        effort=None,
        budget=None,
        history="auto",
        preferences: ThinkingPreferences | None = None,
    ):
        self.runtime = runtime
        self.provider = get_provider(provider).name
        self.model = model
        self.base_url = base_url
        self.limit = max_output_tokens
        self.base = deepcopy(extra) if extra is not None else {}
        self.override = parse_profile(profile or {})
        self.preferences = preferences
        self.recall = preferences is not None
        self.current = normalize_settings(
            self.profile,
            mode=mode,
            effort=effort,
            budget=budget,
            history=history,
        )
        self.runtime.thinking_settings = deepcopy(self.current)

    @property
    def profile(self):
        return thinking_profile(
            self.provider, self.model, base_url=self.base_url, override=self.override
        )

    def native_override(self):
        return native_thinking(self.base)

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
            self.preferences.save(
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
            self.preferences.load(candidate.provider, candidate.model, candidate.base_url)
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
