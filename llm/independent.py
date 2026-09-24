"""Reusable model-catalog policy for standalone, tool-free internal requests."""

from copy import deepcopy
from dataclasses import dataclass

from .model_catalog import model_info
from .thinking import thinking_options

# Unknown maxima are not treated as unlimited and are not increased speculatively.
UNKNOWN_OUTPUT_LIMIT = 8192
MAX_OUTPUT_RETRIES = 4


@dataclass(frozen=True)
class IndependentRequestPolicy:
    provider: str
    model: str
    base_url: str | None
    thinking: dict
    initial_output_tokens: int
    max_output_tokens: int
    output_limit_known: bool

    @classmethod
    def from_config(cls, config):
        info = model_info(config.provider, config.model, allow_snapshot=True)
        maximum = info.max_output_tokens if info else None
        limit = maximum or UNKNOWN_OUTPUT_LIMIT
        initial = min(limit, max(8192, min(32768, limit // 4)))
        settings = deepcopy(info.independent_thinking) if info else {"mode": "auto"}
        # A configured fixed reasoning budget must leave room for the summary body.
        initial = max(initial, settings.get("budget", 0) + 1024)
        if initial > limit:
            raise ValueError("独立请求思考预算过大；请检查 model_catalog 中的配置")
        return cls(
            config.provider,
            config.model,
            getattr(config, "base_url", None),
            settings,
            initial,
            limit,
            maximum is not None,
        )

    def extras(self, output_tokens):
        return thinking_options(
            self.provider,
            self.model,
            **deepcopy(self.thinking),
            max_output_tokens=output_tokens,
            base_url=self.base_url,
        )
