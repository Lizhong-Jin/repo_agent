"""Read display-only token limits from the configured service's model metadata."""

from dataclasses import dataclass
from time import monotonic
from typing import Literal
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class ModelContextLimit:
    tokens: int
    kind: Literal["context", "input"]
    source: str = "服务端自动获取"


def _positive(value):
    return type(value) is int and value > 0


def parse_limit(data, api_format, model):
    if not isinstance(data, dict) or data.get("error"):
        return None
    if api_format not in {"gemini", "anthropic"}:
        # Compatible model catalogues have no standard limit field. Match exactly;
        # never borrow the capacity of another model or infer it from its name.
        entries = data.get("data")
        if not isinstance(entries, list):
            return None
        matches = [item for item in entries if isinstance(item, dict) and item.get("id") == model]
        if len(matches) != 1:
            return None
        data = matches[0]
    elif api_format == "gemini":
        if data.get("name") != "models/" + model.removeprefix("models/"):
            return None
    elif not isinstance(data.get("id"), str):
        return None
    # Anthropic's detail endpoint may resolve an alias to a versioned model ID.
    for key in ("context_length", "context_window", "max_model_len"):
        if _positive(data.get(key)):
            return ModelContextLimit(data[key], "context")
    input_key = "inputTokenLimit" if api_format == "gemini" else "max_input_tokens"
    if _positive(data.get(input_key)):
        return ModelContextLimit(data[input_key], "input")
    # max_tokens/outputTokenLimit are output caps, never context window sizes.
    return None


def fetch_context_limit(http, base_url, headers, api_format, model):
    """One bounded, best-effort GET; no generation, redirects, or retries."""
    path = "/models"
    if api_format in {"gemini", "anthropic"}:
        name = model.removeprefix("models/") if api_format == "gemini" else model
        path += "/" + quote(name, safe="")
    try:
        deadline = monotonic() + 2.0
        with http.stream(
            "GET", base_url + path, headers=headers, timeout=2.0, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                return None
            chunks = bytearray()
            for chunk in response.iter_bytes():
                if monotonic() > deadline or len(chunks) + len(chunk) > 2 * 1024 * 1024:
                    return None
                chunks.extend(chunk)
        data = httpx.Response(200, content=bytes(chunks)).json()
        return parse_limit(data, api_format, model)
    except (httpx.HTTPError, ValueError):
        # Metadata is optional: unsupported endpoints, missing fields and network
        # errors must not prevent normal model requests or expose response secrets.
        return None
