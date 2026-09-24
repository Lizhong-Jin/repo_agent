"""Single source of supported model IDs, thinking controls and documented token limits.

Limits describe the provider's standard API (Qwen: mainland China), not every gateway.
None means unverified, never unlimited. context_kind="input" is an input-only cap.
max_output_tokens is a capability, not the agent's per-request generation budget.
Sources were checked on 2026-09-24; moving aliases and account access can change.

Add a model here; selectors, thinking profiles and context fallback all consume it.
User preferences remain in thinking.json. Do not add model-name rules in consumers.
"""

import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Literal

from .providers import get_provider


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    id: str
    thinking: dict = field(default_factory=dict)
    context_window: int | None = None
    max_output_tokens: int | None = None
    context_kind: Literal["context", "input"] = "context"
    sources: tuple[str, ...] = ()
    checked_on: str = "2026-09-24"
    notes: str = ""
    selectable: bool = True
    dated_snapshots: bool = False
    always_thinking: bool = False

    @property
    def min_thinking(self) -> str:
        """Lowest explicit setting; auto is not a reasoning intensity."""
        modes = self.thinking.get("modes", ("auto",))
        if "disabled" in modes:
            return "disabled"
        efforts = self.thinking.get("efforts", ())
        if efforts:
            return efforts[0]
        budget = self.thinking.get("budget_min")
        if budget is not None:
            return f"budget:{budget}"
        return "enabled" if self.always_thinking or "enabled" in modes else "auto"

    @property
    def max_thinking(self) -> str:
        efforts = self.thinking.get("efforts", ())
        if efforts:
            return efforts[-1]
        if self.thinking.get("budget_min") is not None:
            cap = self.thinking.get("budget_max")
            return f"budget:{cap}" if cap is not None else "budget:unknown"
        modes = self.thinking.get("modes", ("auto",))
        return "enabled" if self.always_thinking or "enabled" in modes else "auto"


# Each registration stores complete per-model records. Shared thinking settings are copied.
MODEL_CATALOG: dict[str, dict[str, ModelInfo]] = {}


def _register(provider, rows, *, thinking, source, **metadata):
    for model, context, output in rows:
        sources = (source,) if isinstance(source, str) else source
        urls = tuple(url.format(model=model, slug=model.replace(".", "-")) for url in sources)
        MODEL_CATALOG.setdefault(provider, {})[model.lower()] = ModelInfo(
            provider, model, deepcopy(thinking), context, output, sources=urls, **metadata
        )


# --------------------------------------------------------------
# openai
# --------------------------------------------------------------
_register(
    "openai",
    [("gpt-5", 400000, 128000), ("gpt-5-mini", 400000, 128000), ("gpt-5-nano", 400000, 128000)],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("minimal", "low", "medium", "high"),
        "default_effort": "medium",
    },
    source="https://developers.openai.com/api/docs/models/{model}",
    dated_snapshots=True,
)

_register(
    "openai",
    [("gpt-5.1", 400000, 128000)],
    thinking={"modes": ("auto", "disabled", "enabled"), "efforts": ("low", "medium", "high")},
    source="https://developers.openai.com/api/docs/models/{model}",
    dated_snapshots=True,
)

_register(
    "openai",
    [("gpt-5.2", 400000, 128000)],
    thinking={
        "modes": ("auto", "disabled", "enabled"),
        "efforts": ("low", "medium", "high", "xhigh"),
    },
    source="https://developers.openai.com/api/docs/models/{model}",
    dated_snapshots=True,
)

_register(
    "openai",
    [("o3", 200000, 100000), ("o4-mini", 200000, 100000)],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("low", "medium", "high"),
        "default_effort": "medium",
    },
    source="https://developers.openai.com/api/docs/models/{model}",
    dated_snapshots=True,
)

_register(
    "openai",
    [
        ("gpt-4.1", 1047576, 32768),
        ("gpt-4.1-mini", 1047576, 32768),
        ("gpt-4o", 128000, 16384),
        ("gpt-4o-mini", 128000, 16384),
    ],
    thinking={"modes": ("auto",)},
    source="https://developers.openai.com/api/docs/models/{model}",
    dated_snapshots=True,
)

# --------------------------------------------------------------
# anthropic
# --------------------------------------------------------------
_register(
    "anthropic",
    [("claude-opus-4-6", 1000000, 128000)],
    thinking={
        "modes": ("auto", "disabled", "adaptive"),
        "efforts": ("low", "medium", "high", "max"),
        "effort_mode": "adaptive",
        "default_effort": "high",
    },
    source="https://platform.claude.com/docs/en/models/opus-4-6/overview",
    dated_snapshots=True,
)

_register(
    "anthropic",
    [("claude-sonnet-4-6", 1000000, 128000)],
    thinking={
        "modes": ("auto", "disabled", "adaptive"),
        "efforts": ("low", "medium", "high"),
        "effort_mode": "adaptive",
        "default_effort": "high",
    },
    source="https://platform.claude.com/docs/en/models/sonnet-4-6/overview",
    dated_snapshots=True,
)

_register(
    "anthropic",
    [("claude-sonnet-4-5", 200000, 64000)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1024},
    source="https://platform.claude.com/docs/de/models/sonnet-4-5/overview",
    dated_snapshots=True,
)

_register(
    "anthropic",
    [("claude-haiku-4-5", 200000, 64000)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1024},
    source="https://platform.claude.com/docs/en/models/haiku-4-5/overview",
    dated_snapshots=True,
)

_register(
    "anthropic",
    [("claude-sonnet-4-0", None, None)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1024},
    source="https://platform.claude.com/docs/en/models/sonnet-4/overview",
    dated_snapshots=True,
    notes="旧型号；本次未核实标准 API 长度限制，保留既有思考适配。",
)

_register(
    "anthropic",
    [("claude-opus-4-0", None, None)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1024},
    source="https://platform.claude.com/docs/en/models/opus-4/overview",
    dated_snapshots=True,
    notes="旧型号；本次未核实标准 API 长度限制，保留既有思考适配。",
)

_register(
    "anthropic",
    [("claude-opus-4-1", None, None)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1024},
    source="https://platform.claude.com/docs/en/models/opus-4-1/overview",
    dated_snapshots=True,
    notes="旧型号；本次未核实标准 API 长度限制，保留既有思考适配。",
)

# --------------------------------------------------------------
# gemini
# --------------------------------------------------------------
_register(
    "gemini",
    [("gemini-3-pro-preview", 1048576, 65536)],
    thinking={"modes": ("auto", "enabled"), "efforts": ("low", "high"), "default_effort": "high"},
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。 官方已于 2026-03-09 下线；仅保留历史配置解析。",
    selectable=False,
)

_register(
    "gemini",
    [("gemini-3-flash-preview", 1048576, 65536)],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("minimal", "low", "medium", "high"),
        "default_effort": "high",
    },
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。",
)

_register(
    "gemini",
    [("gemini-3.1-pro-preview", 1048576, 65536)],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("low", "medium", "high"),
        "default_effort": "high",
    },
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。",
)

_register(
    "gemini",
    [("gemini-2.5-pro", 1048576, 65536)],
    thinking={"modes": ("auto", "enabled"), "budget_min": 128, "budget_max": 32768},
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。",
)

_register(
    "gemini",
    [("gemini-2.5-flash", 1048576, 65536)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1, "budget_max": 24576},
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。",
)

_register(
    "gemini",
    [("gemini-2.5-flash-lite", 1048576, 65536)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 512, "budget_max": 24576},
    source="https://ai.google.dev/gemini-api/docs/models/{model}",
    context_kind="input",
    notes="Google 公布的是输入上限，不是输入与输出之和。",
)

# --------------------------------------------------------------
# qwen
# --------------------------------------------------------------
_register(
    "qwen",
    [
        ("qwen-plus", 1000000, 32768),
        ("qwen3.5-plus", 1000000, 65536),
        ("qwen3.5-flash", 1000000, 65536),
    ],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1, "budget_max": 81920},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [
        ("qwen-flash", 1000000, 32768),
        ("qwen-turbo", 131072, 16384),
        ("qwen3-32b", 131072, 8192),
        ("qwen3-8b", 131072, 8192),
        ("qwen3-14b", 131072, 8192),
        ("qwen3-30b-a3b", 131072, 8192),
    ],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [("qwen3-235b-a22b", 131072, 16384)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1, "budget_max": 38912},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [("qwen3.8-max", 1000000, 131072), ("qwen3.8-flash", 1000000, 131072)],
    thinking={"modes": ("auto", "disabled", "enabled"), "budget_min": 1, "budget_max": 262144},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [("qwen3-next-80b-a3b-thinking", 131072, 32768)],
    thinking={"modes": ("auto", "enabled"), "budget_min": 1},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [("qwen3-235b-a22b-thinking-2507", 131072, 32768)],
    thinking={"modes": ("auto", "enabled"), "budget_min": 1, "budget_max": 81920},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
)

_register(
    "qwen",
    [("qwen3-30b-a3b-thinking-2507", 81920, 32768)],
    thinking={"modes": ("auto", "enabled"), "budget_min": 1},
    source="https://help.aliyun.com/zh/model-studio/{slug}",
    notes="官方页面上下文 81920 小于其列出的最大输入 126976; 暂取明确上下文字段，服务端元数据优先。",
)

# --------------------------------------------------------------
# zhipu
# --------------------------------------------------------------
_register(
    "zhipu",
    [("glm-5.3", 1000000, 131072)],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("low", "high", "max"),
        "history": True,
        "default_effort": "max",
        "aliases": {"minimal": "low", "medium": "high", "xhigh": "max"},
    },
    source="https://docs.bigmodel.cn/cn/guide/models/text/{model}",
)

_register(
    "zhipu",
    [
        ("glm-5.3-flash", 1000000, 131072),
        ("glm-5.3-highspeed", 1000000, 131072),
        ("glm-5.3-flashx", 1000000, 131072),
    ],
    thinking={
        "modes": ("auto", "enabled"),
        "efforts": ("low", "high", "max"),
        "history": True,
        "default_effort": "max",
        "aliases": {"minimal": "low", "medium": "high", "xhigh": "max"},
    },
    source="https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3",
    notes="保留项目已有思考适配；此变体未核实独立长度规格，不继承同系列上限。",
)

_register(
    "zhipu",
    [("glm-5.2", 1000000, 131072)],
    thinking={
        "modes": ("auto", "disabled", "enabled"),
        "efforts": ("high", "max"),
        "history": True,
        "default_effort": "max",
        "aliases": {"low": "high", "medium": "high", "xhigh": "max"},
    },
    source="https://docs.bigmodel.cn/cn/guide/models/text/{model}",
)

_register(
    "zhipu",
    [("glm-5.2-highspeed", None, None)],
    thinking={
        "modes": ("auto", "disabled", "enabled"),
        "efforts": ("high", "max"),
        "history": True,
        "default_effort": "max",
        "aliases": {"low": "high", "medium": "high", "xhigh": "max"},
    },
    source="https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2",
    notes="保留项目已有思考适配；此变体未核实独立长度规格，不继承同系列上限。",
)

_register(
    "zhipu",
    [("glm-4.7", 200000, 131072), ("glm-5", 200000, 131072), ("glm-5.1", 200000, 131072)],
    thinking={"modes": ("auto", "disabled", "enabled"), "history": True},
    source="https://docs.bigmodel.cn/cn/guide/models/text/{model}",
)

_register(
    "zhipu",
    [("glm-5-turbo", None, None)],
    thinking={"modes": ("auto", "disabled", "enabled"), "history": True},
    source="https://docs.bigmodel.cn/cn/guide/models/text/glm-5",
    notes="保留项目已有思考适配; 此变体未核实独立长度规格，不继承同系列上限。",
)

# --------------------------------------------------------------
# deepseek
# --------------------------------------------------------------
_register(
    "deepseek",
    [("deepseek-flash", 1000000, 393216), ("deepseek-v4-pro", 1000000, 393216)],
    thinking={
        "modes": ("auto", "disabled", "enabled"),
        "efforts": ("low", "high", "max"),
        "default_effort": "high",
        "aliases": {"minimal": "low", "medium": "high", "xhigh": "high"},
    },
    source=(
        "https://api-docs.deepseek.com/api/create-chat-completion/",
        "https://api-docs.deepseek.com/quick_start/pricing/",
        "https://api-docs.deepseek.com/guides/thinking_mode/",
    ),
    notes="384K = 393216; 上下文按官方价格页的 1M 记录为 1000000。",
)

# --------------------------------------------------------------
# moonshot
# --------------------------------------------------------------
_register(
    "moonshot",
    [("kimi-k2.6", 262144, None)],
    thinking={"modes": ("auto", "disabled", "enabled")},
    source=(
        "https://platform.kimi.com/docs/guide/kimi-k2-6-quickstart",
        "https://platform.kimi.com/docs/models",
    ),
    notes="上下文来源 https://platform.kimi.com/docs/models; 32768 是默认输出量，非已确认最大值。",
)

# --------------------------------------------------------------
# minimax
# --------------------------------------------------------------
_register(
    "minimax",
    [
        ("MiniMax-M2.5", 204800, None),
        ("MiniMax-M2.5-highspeed", 204800, None),
        ("MiniMax-M2.1", 204800, None),
        ("MiniMax-M2.1-highspeed", 204800, None),
        ("MiniMax-M2", 204800, None),
    ],
    thinking={"modes": ("auto",)},
    always_thinking=True,
    source="https://platform.minimax.cn/docs/api-reference/text-anthropic-api",
    notes="M2 系列始终思考；当前 OpenAI 兼容适配只使用默认行为。最大输出尚未核实。",
)


def supported_providers() -> tuple[str, ...]:
    return tuple(p for p in MODEL_CATALOG if supported_models(p))


def supported_models(provider: str) -> tuple[ModelInfo, ...]:
    name = get_provider(provider).name
    return tuple(deepcopy(m) for m in MODEL_CATALOG.get(name, {}).values() if m.selectable)


def model_info(provider: str, model: str, *, allow_snapshot=False) -> ModelInfo | None:
    """Exact catalogue lookup; inferred dated profiles never inherit token limits."""
    name = get_provider(provider).name
    key = model.lower()
    models = MODEL_CATALOG.get(name, {})
    if key in models:
        return deepcopy(models[key])
    if allow_snapshot:
        for entry in models.values():
            if entry.dated_snapshots and re.fullmatch(
                re.escape(entry.id.lower()) + r"-\d{4}-?\d{2}-?\d{2}", key
            ):
                return replace(
                    deepcopy(entry),
                    id=model,
                    context_window=None,
                    max_output_tokens=None,
                    selectable=False,
                    notes="日期快照只继承思考规则；未登记长度，不在选择列表中。",
                )
    return None


def require_supported_model(provider: str, model: str) -> ModelInfo:
    info = model_info(provider, model)
    if info is None or not info.selectable:
        raise ValueError("只能选择模型目录中已支持的模型；请从列表选择")
    return info
