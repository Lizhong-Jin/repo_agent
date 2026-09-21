"""Read only provider-returned display text, never encrypted or redacted reasoning."""


def visible_thinking(api_format, data):
    if api_format == "chat_completions":
        message = (data.get("choices") or [{}])[0].get("message", {})
        for field in ("reasoning_content", "reasoning", "reasoning_text"):
            value = message.get(field)
            if isinstance(value, str) and value:
                yield value
                break
    elif api_format == "anthropic":
        for block in data.get("content", []):
            if block.get("type") == "thinking" and isinstance(block.get("thinking"), str):
                yield block["thinking"]
    elif api_format == "responses":
        for item in data.get("output", []):
            if item.get("type") == "reasoning":
                for part in item.get("summary", []):
                    if part.get("type") == "summary_text" and isinstance(part.get("text"), str):
                        yield part["text"]
    elif api_format == "gemini":
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("thought") and isinstance(part.get("text"), str):
                    yield part["text"]


def request_thinking_summary(body, api_format, profile):
    """Request summaries for registered models without changing effort or budget.

    Explicit native settings win. Unknown models can use native extras to opt in.
    """
    if not profile.known:
        return
    if api_format == "responses" and profile.efforts:
        reasoning = body.setdefault("reasoning", {})
        if isinstance(reasoning, dict) and reasoning.get("effort") != "none":
            reasoning.setdefault("summary", "auto")
    elif api_format == "anthropic":
        thinking = body.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "adaptive"}:
            thinking.setdefault("display", "summarized")
    elif api_format == "gemini" and (profile.efforts or profile.budget_min):
        config = body.setdefault("generationConfig", {}).setdefault("thinkingConfig", {})
        if isinstance(config, dict) and config.get("thinkingBudget") != 0:
            config.setdefault("includeThoughts", True)


def has_thinking_state(api_format, state):
    if state is None:
        return False
    payload = state.payload
    if api_format in {"anthropic", "responses"}:
        return any(
            block.get("type") in {"thinking", "redacted_thinking", "reasoning"} for block in payload
        )
    if api_format == "gemini":
        return any(
            part.get("thought") or "thoughtSignature" in part for part in payload.get("parts", [])
        )
    if api_format == "chat_completions":
        return bool(payload.get("reasoning_details"))
    return False
