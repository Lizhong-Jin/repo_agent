from ..errors import InvalidRequestError, InvalidResponseError
from ..schemas import LLMRequest, LLMResponse
from .base import Adapter, count, tool_call, usage


class AnthropicAdapter(Adapter):
    def path(self) -> str:
        return "/messages"

    def encode(self, request: LLMRequest) -> dict:
        system, messages = [], []
        for m in request.messages:
            if m.role == "system":
                system.append(m.content)
                continue
            role = "user" if m.role == "tool" else m.role
            blocks = self.state(m)
            if blocks is None:
                blocks = []
                if m.role == "tool":
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                            "is_error": m.is_error,
                        }
                    )
                else:
                    if m.content:
                        blocks.append({"type": "text", "text": m.content})
                    blocks.extend(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                        for c in m.tool_calls
                    )
            # Parallel tool results must share the immediately following user turn.
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
        body = {"model": self.model, "messages": messages, "max_tokens": request.max_output_tokens}
        if system:
            body["system"] = "\n\n".join(system)
        if request.temperature is not None:
            if request.temperature > 1:
                raise InvalidRequestError("Anthropic temperature must be between 0 and 1")
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            body["tool_choice"] = {
                "type": {"auto": "auto", "none": "none", "required": "any"}[request.tool_choice]
            }
        return self.extras(body, request)

    def decode(self, data: dict) -> LLMResponse:
        if data.get("role") != "assistant":
            raise InvalidResponseError("Expected an Anthropic assistant response")
        blocks = data["content"]
        texts, calls = [], []
        for b in blocks:
            if b["type"] == "text":
                texts.append(b["text"])
            elif b["type"] == "tool_use":
                calls.append(tool_call(b["id"], b["name"], b["input"]))
            elif b["type"] not in {"thinking", "redacted_thinking"}:
                raise InvalidResponseError("Unsupported Anthropic content block")
        finish = data.get("stop_reason")
        reason = {
            "end_turn": "stop",
            "stop_sequence": "stop",
            "tool_use": "tool_calls",
            "max_tokens": "length",
            "refusal": "blocked",
        }.get(finish, "other")
        u = data.get("usage") or {}
        input_tokens = count(u, "input_tokens")
        cached = count(u, "cache_read_input_tokens")
        written = count(u, "cache_creation_input_tokens")
        # Anthropic's input_tokens excludes cache reads/writes; normalize to total input.
        if input_tokens is not None:
            input_tokens += (cached or 0) + (written or 0)
        tokens = usage(
            input_tokens,
            count(u, "output_tokens"),
            cached_input_tokens=cached,
            cache_write_tokens=written,
        )
        return self.response(
            data, self.assistant("".join(texts), calls, blocks), reason, tokens, finish
        )
