"""Private per-model thinking preferences. No API keys or conversation contents."""

import json
import os
import tempfile
from pathlib import Path

from llm import ConfigurationError
from llm.providers import get_provider
from llm.thinking_profiles import endpoint, parse_profile

from .config_storage import config_lock, read_bytes
from .installation import user_config_path
from .settings import native_thinking


def preference_path():
    return user_config_path().with_name("thinking.json")


def identity(provider, model, base_url=None):
    # Keep model IDs case-sensitive, matching the request's actual model selector.
    return json.dumps(
        [get_provider(provider).name, endpoint(provider, base_url), model],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _read(path):
    raw = read_bytes(path)
    if raw is None:
        return {"version": 1, "models": {}}
    try:
        if len(raw) > 1024 * 1024:
            raise ValueError
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("models"), dict)
        ):
            raise ValueError
        return data
    except (ValueError, UnicodeError):
        raise ConfigurationError(
            f"思考偏好文件格式无效：{path}；请修复文件或关闭 LLM_THINKING_RECALL"
        ) from None


def load_preference(provider, model, base_url=None):
    value = _read(preference_path())["models"].get(identity(provider, model, base_url))
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"settings", "profile"}
        or not isinstance(value["settings"], dict)
        or set(value["settings"]) != {"mode", "effort", "budget", "history"}
    ):
        raise ConfigurationError("当前模型的思考偏好格式无效；请修复 thinking.json")
    s = value["settings"]
    if (
        s["mode"] not in ("auto", "enabled", "disabled", "adaptive")
        or s["history"] not in ("auto", "on", "off")
        or (s["effort"] is not None and not isinstance(s["effort"], str))
        or (s["budget"] is not None and type(s["budget"]) is not int)
    ):
        raise ConfigurationError("当前模型的思考偏好字段无效；请修复 thinking.json")
    parse_profile(value["profile"])
    return value


def save_preference(provider, model, base_url, settings, profile, *, forget=False):
    path = preference_path()
    with config_lock(path):
        data = _read(path)
        key = identity(provider, model, base_url)
        if forget:
            data["models"].pop(key, None)
        else:
            data["models"][key] = {"settings": settings, "profile": profile}
        content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
        if len(content) > 1024 * 1024:
            raise ConfigurationError("思考偏好文件过大，请清理不再使用的模型偏好")
        fd, temp = tempfile.mkstemp(prefix=".thinking-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)


def restore_thinking_args(args):
    """CLI explicit group / configured nondefault settings > saved model preference > auto."""
    if not getattr(args, "thinking_recall", True) or getattr(args, "thinking_explicit", False):
        return
    try:
        extra = json.loads(args.extra_json)
    except (ValueError, TypeError):
        raise ConfigurationError("LLM_EXTRA_JSON must be a JSON object") from None
    if not isinstance(extra, dict):
        raise ConfigurationError("LLM_EXTRA_JSON must be a JSON object")
    if (
        args.thinking != "auto"
        or args.reasoning_effort is not None
        or args.thinking_budget is not None
        or getattr(args, "thinking_history", "auto") != "auto"
        or parse_profile(getattr(args, "thinking_profile", "{}"))
        or native_thinking(extra)
    ):
        return
    saved = load_preference(args.provider, args.model, getattr(args, "base_url", None))
    if saved:
        for key, field in (
            ("mode", "thinking"),
            ("effort", "reasoning_effort"),
            ("budget", "thinking_budget"),
            ("history", "thinking_history"),
        ):
            setattr(args, field, saved["settings"][key])
        args.thinking_profile = json.dumps(saved["profile"])
