from ..errors import InvalidRequestError, InvalidResponseError
from ..schemas import LLMRequest, LLMResponse, Message, json_string
from .base import Adapter, count, tool_call, usage


class ChatCompletionsAdapter(Adapter):
    """Shared OpenAI-compatible wire format for the mainland provider presets."""

    def path(self) -> str:
        return "/chat/completions"

    def encode(self, request: LLMRequest) -> dict:
        messages = []
        for m in request.messages:
            native = self.state(m)
            if native is not None:
                messages.append(native)
                continue
            item = {"role": m.role, "content": m.content}
            if m.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json_string(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            if m.role == "tool":
                item["tool_call_id"] = m.tool_call_id
                # Chat Completions has no standard is_error field.
                if m.is_error:
                    item["content"] = json_string({"error": m.content})
            messages.append(item)
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            if self.provider == "zhipu" and request.tool_choice != "auto":
                raise InvalidRequestError("The Zhipu preset supports only tool_choice=auto")
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
            body["tool_choice"] = request.tool_choice
        if self.provider == "minimax":
            body["reasoning_split"] = True
        return self.extras(body, request)

    def decode(self, data: dict) -> LLMResponse:
        choices = data["choices"]
        if len(choices) != 1:
            raise InvalidResponseError("Expected exactly one completion candidate")
        choice = choices[0]
        native = choice["message"]
        if native.get("role") != "assistant":
            raise InvalidResponseError("Expected an assistant message")
        limited = choice.get("finish_reason") == "length" and not native.get("refusal")
        truncated = limited and bool(native.get("tool_calls"))
        calls = []
        for c in [] if limited else native.get("tool_calls") or []:
            if c.get("type") != "function":
                raise InvalidResponseError("Unsupported tool call type")
            calls.append(tool_call(c["id"], c["function"]["name"], c["function"]["arguments"]))
        text = native.get("content") or ""
        refusal = native.get("refusal")
        if not text and refusal:
            text = refusal
        message = self.assistant(text, calls, native)
        if limited:
            message = Message("assistant", text)
        finish = choice.get("finish_reason")
        reason = {
            "stop": "stop",
            "tool_calls": "tool_calls",
            "length": "length",
            "content_filter": "blocked",
            "sensitive": "blocked",
        }.get(finish, "other")
        if refusal:
            reason = "blocked"
        elif calls and reason == "stop":
            reason = "tool_calls"
        u = data.get("usage") or {}
        cached = count(u.get("prompt_tokens_details") or {}, "cached_tokens")
        if cached is None:
            cached = count(u, "prompt_cache_hit_tokens")
        if cached is None:
            cached = count(u, "cached_tokens")
        tokens = usage(
            count(u, "prompt_tokens"),
            count(u, "completion_tokens"),
            total_tokens=count(u, "total_tokens"),
            cached_input_tokens=cached,
            reasoning_tokens=count(u.get("completion_tokens_details") or {}, "reasoning_tokens"),
        )
        return self.response(data, message, reason, tokens, finish, truncated_tool_calls=truncated)
