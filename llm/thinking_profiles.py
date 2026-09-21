"""Model thinking capabilities; no network probes or automatic model substitution."""

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError
from .providers import get_provider
from .thinking_catalog import MODEL_THINKING_PROFILES

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
MODES = ("auto", "disabled", "enabled", "adaptive")


@dataclass(frozen=True)
class ThinkingProfile:
    modes: tuple[str, ...] = ("auto",)
    efforts: tuple[str, ...] = ()
    effort_mode: str = "enabled"
    budget_min: int | None = None
    budget_max: int | None = None
    history: bool = False
    default_effort: str | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    source: str = "通用厂商规则（模型能力未确认）"
    known: bool = False


def endpoint(provider, base_url=None):
    spec = get_provider(provider)
    url = urlsplit((base_url or "").strip() or spec.base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ConfigurationError("模型接口地址必须为不含凭据、查询参数的 HTTP(S) 地址")
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path.rstrip("/"), "", ""))


def history_default(provider, base_url=None):
    url = urlsplit(endpoint(provider, base_url))
    if get_provider(provider).name == "zhipu" and url.hostname in {"open.bigmodel.cn", "api.z.ai"}:
        return "保留" if "/coding/" in url.path else "清除跨轮思考"
    return "由接口决定"


def parse_profile(value):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise ConfigurationError("LLM_THINKING_PROFILE 必须为 JSON 对象") from None
    if not isinstance(result, dict):
        raise ConfigurationError("LLM_THINKING_PROFILE 必须为 JSON 对象")
    allowed = {
        "modes",
        "efforts",
        "effort_mode",
        "budget_min",
        "budget_max",
        "history",
        "default_effort",
        "aliases",
    }
    if result.keys() - allowed:
        raise ConfigurationError("LLM_THINKING_PROFILE 包含未知能力字段")
    for key, allowed_values in (("modes", MODES), ("efforts", EFFORTS)):
        if key in result and (
            not isinstance(result[key], list)
            or any(not isinstance(v, str) or v not in allowed_values for v in result[key])
            or len(set(result[key])) != len(result[key])
        ):
            raise ConfigurationError(f"思考能力 {key} 必须为不重复的合法选项列表")
    if "modes" in result and "auto" not in result["modes"]:
        raise ConfigurationError("思考能力 modes 必须包含 auto")
    if "effort_mode" in result and result["effort_mode"] not in {"enabled", "adaptive"}:
        raise ConfigurationError("effort_mode 必须为 enabled 或 adaptive")
    for key in ("budget_min", "budget_max"):
        if (
            key in result
            and result[key] is not None
            and (type(result[key]) is not int or result[key] <= 0)
        ):
            raise ConfigurationError(f"思考能力 {key} 必须为正整数或 null")
    if "history" in result and type(result["history"]) is not bool:
        raise ConfigurationError("思考能力 history 必须为布尔值")
    if "default_effort" in result and result["default_effort"] not in (*EFFORTS, None):
        raise ConfigurationError("default_effort 必须为合法强度或 null")
    aliases = result.get("aliases", {})
    if not isinstance(aliases, dict) or any(
        k not in EFFORTS or v not in EFFORTS for k, v in aliases.items()
    ):
        raise ConfigurationError("思考能力 aliases 必须映射合法的强度名称")
    return result.copy()


def _builtin_profile(provider, model):
    groups = MODEL_THINKING_PROFILES.get(provider, {})
    # Explicit snapshots can override a family rule regardless of dictionary order.
    for dated in (False, True):
        for models, settings in groups.items():
            if dated:
                matches = settings.get("dated_snapshots", False) and any(
                    re.fullmatch(re.escape(name) + r"-\d{4}-?\d{2}-?\d{2}", model)
                    for name in models
                )
            else:
                matches = model in models
            if matches:
                # Returned profiles must not expose mutable catalog dictionaries.
                return deepcopy({k: v for k, v in settings.items() if k != "dated_snapshots"})
    return None


def thinking_profile(provider, model, *, base_url=None, override=None):
    name = get_provider(provider).name
    kwargs = _builtin_profile(name, model.lower())
    profile = (
        ThinkingProfile(**kwargs, source="内置模型规则", known=True)
        if kwargs is not None
        else ThinkingProfile()
    )
    custom = parse_profile({} if override is None else override)
    if custom:
        for key in ("modes", "efforts"):
            if key in custom:
                custom[key] = tuple(custom[key])
        profile = replace(profile, **custom, known=True, source="自定义模型规则")
    if profile.efforts and profile.effort_mode not in profile.modes:
        raise ConfigurationError("思考能力 efforts 要求 modes 包含 effort_mode")
    if any(target not in profile.efforts for target in profile.aliases.values()):
        raise ConfigurationError(
            "思考别名必须指向当前模型的有效强度；覆盖 efforts 时请一并覆盖 aliases"
        )
    if profile.budget_max is not None and (
        profile.budget_min is None or profile.budget_max < profile.budget_min
    ):
        raise ConfigurationError("思考预算上限不能小于下限")
    if profile.history and name != "zhipu":
        raise ConfigurationError("目前仅智谱适配历史思考保留开关")
    # Validate endpoint even for profiles that do not use an endpoint-specific option.
    endpoint(name, base_url)
    return profile
