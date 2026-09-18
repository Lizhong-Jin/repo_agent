from urllib.parse import quote
from uuid import uuid4

from ..errors import InvalidResponseError
from ..schemas import LLMRequest, LLMResponse
from .base import Adapter, count, tool_call, usage


class GeminiAdapter(Adapter):
    """Google generateContent REST API; original parts retain thoughtSignature fields."""

    def path(self) -> str:
        model = self.model.removeprefix("models/")
        return f"/models/{quote(model, safe='')}:generateContent"

    def encode(self, request: LLMRequest) -> dict:
        system, contents = [], []
        native_ids: dict[str, str | None] = {}
        for m in request.messages:
            if m.role == "system":
                system.append({"text": m.content})
                continue
            native = self.state(m)
            if native is not None:
                parts = native["parts"]
                wire_calls = [p["functionCall"] for p in parts if "functionCall" in p]
                for call, wire in zip(m.tool_calls, wire_calls, strict=True):
                    native_ids[call.id] = wire.get("id")
                contents.append(native)
                continue
            role = "model" if m.role == "assistant" else "user"
            parts = []
            if m.role == "tool":
                result = {
                    "name": m.name,
                    "response": {"error" if m.is_error else "result": m.content},
                }
                # Some models do not issue IDs. Never send a synthetic ID back to them.
                if native_ids.get(m.tool_call_id):
                    result["id"] = native_ids[m.tool_call_id]
                parts.append({"functionResponse": result})
            else:
                if m.content:
                    parts.append({"text": m.content})
                for c in m.tool_calls:
                    native_ids[c.id] = c.id
                    parts.append(
                        {"functionCall": {"id": c.id, "name": c.name, "args": c.arguments}}
                    )
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})
        body = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": request.max_output_tokens, "candidateCount": 1},
        }
        if system:
            body["systemInstruction"] = {"parts": system}
        if request.temperature is not None:
            body["generationConfig"]["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parametersJsonSchema": t.parameters,
                        }
                        for t in request.tools
                    ]
                }
            ]
            body["toolConfig"] = {
                "functionCallingConfig": {
                    "mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}[request.tool_choice]
                }
            }
        return self.extras(body, request)

    def decode(self, data: dict) -> LLMResponse:
        u = data.get("usageMetadata") or {}
        output_tokens = count(u, "candidatesTokenCount")
        reasoning_tokens = count(u, "thoughtsTokenCount")
        if output_tokens is not None:
            output_tokens += reasoning_tokens or 0
        tokens = usage(
            count(u, "promptTokenCount"),
            output_tokens,
            total_tokens=count(u, "totalTokenCount"),
            cached_input_tokens=count(u, "cachedContentTokenCount"),
            reasoning_tokens=reasoning_tokens,
        )
        candidates = data.get("candidates") or []
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if not candidates and block_reason:
            return self.response(
                data,
                self.assistant("", [], {"role": "model", "parts": []}),
                "blocked",
                tokens,
                block_reason,
            )
        if len(candidates) != 1:
            raise InvalidResponseError("Expected exactly one Gemini candidate")
        candidate = candidates[0]
        native = candidate.get("content") or {"role": "model", "parts": []}
        texts, calls = [], []
        for part in native.get("parts", []):
            if "functionCall" in part:
                c = part["functionCall"]
                calls.append(
                    tool_call(c.get("id") or f"call_{uuid4().hex}", c["name"], c.get("args", {}))
                )
            elif "text" in part:
                if not part.get("thought"):
                    texts.append(part["text"])
            elif set(part) - {"thoughtSignature", "thought"}:
                raise InvalidResponseError("Unsupported Gemini response part")
        finish = candidate.get("finishReason")
        reason = {
            "STOP": "tool_calls" if calls else "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "blocked",
            "RECITATION": "blocked",
            "BLOCKLIST": "blocked",
            "PROHIBITED_CONTENT": "blocked",
            "SPII": "blocked",
        }.get(finish, "other")
        return self.response(
            data, self.assistant("".join(texts), calls, native), reason, tokens, finish
        )
