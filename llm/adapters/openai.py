from ..errors import InvalidResponseError, ProviderError
from ..schemas import LLMRequest, LLMResponse, Message, json_string
from .base import Adapter, count, tool_call, usage


class OpenAIResponsesAdapter(Adapter):
    def path(self) -> str:
        return "/responses"

    def encode(self, request: LLMRequest) -> dict:
        items = []
        for m in request.messages:
            native = self.state(m)
            if native is not None:
                items.extend(native)
                continue
            if m.role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m.tool_call_id,
                        "output": json_string({"error": m.content}) if m.is_error else m.content,
                    }
                )
                continue
            if m.content:
                items.append({"role": m.role, "content": m.content})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": c.id,
                    "name": c.name,
                    "arguments": json_string(c.arguments),
                }
                for c in m.tool_calls
            )
        body = {
            "model": self.model,
            "input": items,
            "max_output_tokens": request.max_output_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            # Explicit non-strict mode preserves optional properties in the common schema.
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": False,
                }
                for t in request.tools
            ]
            body["tool_choice"] = request.tool_choice
        body = self.extras(body, request)
        if not isinstance(body["include"], list) or not all(
            isinstance(item, str) for item in body["include"]
        ):
            from ..errors import InvalidRequestError

            raise InvalidRequestError("OpenAI extra.include must be a list of strings")
        if "reasoning.encrypted_content" not in body["include"]:
            body["include"].append("reasoning.encrypted_content")
        return body

    def decode(self, data: dict) -> LLMResponse:
        status = data.get("status")
        if status == "failed" or data.get("error"):
            raise ProviderError("OpenAI response failed", provider=self.provider)
        if status not in {"completed", "incomplete"}:
            raise InvalidResponseError("Expected a completed or incomplete synchronous response")
        limited = (
            status == "incomplete"
            and (data.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
        )
        truncated = False
        output = data["output"]
        texts, calls = [], []
        blocked = False
        for item in output:
            if item["type"] == "message":
                for block in item["content"]:
                    if block["type"] == "output_text":
                        texts.append(block["text"])
                    elif block["type"] == "refusal":
                        blocked = True
                        texts.append(block["refusal"])
                    else:
                        raise InvalidResponseError("Unsupported OpenAI message content")
            elif item["type"] == "function_call":
                if limited:
                    truncated = True
                else:
                    calls.append(tool_call(item["call_id"], item["name"], item["arguments"]))
            elif item["type"] != "reasoning":
                raise InvalidResponseError(
                    "Only text, reasoning and custom function tools are supported"
                )
        finish = status
        reason = "tool_calls" if calls else "stop"
        if status == "incomplete":
            finish = (data.get("incomplete_details") or {}).get("reason", "incomplete")
            reason = {"max_output_tokens": "length", "content_filter": "blocked"}.get(
                finish, "other"
            )
        if blocked:
            reason = "blocked"
        u = data.get("usage") or {}
        tokens = usage(
            count(u, "input_tokens"),
            count(u, "output_tokens"),
            total_tokens=count(u, "total_tokens"),
            cached_input_tokens=count(u.get("input_tokens_details") or {}, "cached_tokens"),
            reasoning_tokens=count(u.get("output_tokens_details") or {}, "reasoning_tokens"),
        )
        message = (
            Message("assistant", "".join(texts))
            if limited
            else self.assistant("".join(texts), calls, output)
        )
        return self.response(data, message, reason, tokens, finish, truncated_tool_calls=truncated)
