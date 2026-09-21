"""Incrementally assemble SSE responses before validating/executing any tool calls."""

import json
from copy import deepcopy

from .errors import InvalidResponseError, ProviderError


class StreamAssembler:
    def __init__(
        self, api_format: str, on_text=None, on_thinking=None, on_thinking_end=None
    ) -> None:
        self.on_text = on_text
        self.on_thinking = on_thinking
        self.on_thinking_end = on_thinking_end
        self.api_format = api_format
        self.data: dict = {}
        self.lines: list[str] = []
        self.done = False
        self.calls: dict[int, dict] = {}
        self.blocks: dict[int, dict] = {}
        self.arguments: dict[int, str] = {}
        self.open_blocks: set[int] = set()

    def feed(self, line: str) -> None:
        if line.startswith("data:"):
            self.lines.append(line[5:].removeprefix(" "))
        elif not line and self.lines:
            value = "\n".join(self.lines)
            self.lines.clear()
            if value == "[DONE]":
                if self.api_format != "chat_completions":
                    raise InvalidResponseError("Unexpected stream terminator")
                self.done = True
                return
            try:
                event = json.loads(value)
                if not isinstance(event, dict):
                    raise ValueError
                if event.get("error") or event.get("type") in {"error", "response.failed"}:
                    raise ProviderError("Provider returned a stream error")
                if (event.get("base_resp") or {}).get("status_code", 0):
                    raise ProviderError("Provider returned a stream application error")
                self._event(event)
                fragments = self._text(event)
                thoughts = self._thinking(event)
                thinking_ended = self._thinking_ended(event)
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                raise InvalidResponseError("Invalid provider stream event") from None
            if self.api_format == "gemini":
                for candidate in event.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        if "text" in part:
                            callback = self.on_thinking if part.get("thought") else self.on_text
                            if callback:
                                callback(part["text"])
                        elif "functionCall" in part and self.on_thinking_end:
                            self.on_thinking_end()
            else:
                if self.on_thinking:
                    for fragment in thoughts:
                        if fragment:
                            self.on_thinking(fragment)
                if thinking_ended and self.on_thinking_end:
                    self.on_thinking_end()
                if self.on_text:
                    for fragment in fragments:
                        self.on_text(fragment)

    def _thinking(self, event):
        if self.api_format == "chat_completions":
            choices = event.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            # Never render opaque state/signatures; choose one compatible text field.
            for name in ("reasoning_content", "reasoning", "reasoning_text"):
                value = delta.get(name)
                if isinstance(value, str) and value:
                    return [value]
        elif self.api_format == "responses":
            if event.get("type") == "response.reasoning_summary_text.delta":
                return [event.get("delta", "")]
        elif self.api_format == "anthropic":
            if event.get("type") == "content_block_start":
                block = event["content_block"]
                return [block.get("thinking", "")] if block["type"] == "thinking" else []
            if event.get("type") == "content_block_delta":
                delta = event["delta"]
                return [delta.get("thinking", "")] if delta["type"] == "thinking_delta" else []
        elif self.api_format == "gemini":
            return [
                part["text"]
                for candidate in event.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
                if "text" in part and part.get("thought")
            ]
        return []

    def _thinking_ended(self, event):
        if self.api_format == "chat_completions":
            choice = (event.get("choices") or [{}])[0]
            return bool(choice.get("finish_reason") or choice.get("delta", {}).get("tool_calls"))
        if self.api_format == "anthropic" and event.get("type") == "content_block_stop":
            return self.blocks[event["index"]]["type"] == "thinking"
        if self.api_format == "responses":
            return event.get("type") in {
                "response.reasoning_summary_text.done",
                "response.output_item.done",
            }
        if self.api_format == "gemini":
            return any(
                "functionCall" in part
                for candidate in event.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
            )
        return False

    def _text(self, event):
        if self.api_format == "chat_completions":
            choices = event.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            return [delta.get("content") or delta.get("refusal") or ""]
        if self.api_format == "responses":
            if event.get("type") in {"response.output_text.delta", "response.refusal.delta"}:
                return [event.get("delta", "")]
        elif self.api_format == "anthropic":
            if event.get("type") == "content_block_start":
                block = event["content_block"]
                return [block.get("text", "")] if block["type"] == "text" else []
            if event.get("type") == "content_block_delta":
                delta = event["delta"]
                return [delta.get("text", "")] if delta["type"] == "text_delta" else []
        elif self.api_format == "gemini":
            return [
                part["text"]
                for candidate in event.get("candidates", [])
                for part in candidate.get("content", {}).get("parts", [])
                if "text" in part and not part.get("thought")
            ]
        return []

    def _event(self, event: dict) -> None:
        if self.done:
            raise InvalidResponseError("Received data after stream completion")
        if self.api_format == "chat_completions":
            for key in ("id", "model", "usage"):
                if event.get(key) is not None:
                    self.data[key] = event[key]
            choices = event.get("choices", [])
            if not choices:
                return
            if len(choices) != 1 or choices[0].get("index", 0) != 0:
                raise InvalidResponseError("Expected one streamed candidate")
            choice = choices[0]
            target = self.data.setdefault("choices", [{"message": {"role": "assistant"}}])[0]
            native = target["message"]
            for key, value in choice.get("delta", {}).items():
                if key == "tool_calls":
                    for call in value or []:
                        index = call["index"]
                        if type(index) is not int or index < 0:
                            raise ValueError
                        accumulated = self.calls.setdefault(index, {"function": {}})
                        for field in ("id", "type"):
                            if call.get(field):
                                accumulated[field] = call[field]
                        for field, fragment in call.get("function", {}).items():
                            accumulated["function"][field] = (
                                accumulated["function"].get(field, "") + fragment
                            )
                elif value is not None:
                    if key == "role":
                        native[key] = value
                    elif isinstance(value, str):
                        native[key] = native.get(key, "") + value
                    elif isinstance(value, list):
                        native.setdefault(key, []).extend(value)
                    else:
                        native[key] = value
            if choice.get("finish_reason"):
                target["finish_reason"] = choice["finish_reason"]
        elif self.api_format == "responses":
            if event.get("type") in {"response.completed", "response.incomplete"}:
                self.data = event["response"]
                self.done = True
        elif self.api_format == "anthropic":
            kind = event["type"]
            if kind == "message_start":
                self.data = deepcopy(event["message"])
            elif kind == "content_block_start":
                index = event["index"]
                if type(index) is not int or index < 0 or index in self.blocks:
                    raise InvalidResponseError("Invalid content block index")
                self.open_blocks.add(index)
                self.blocks[index] = deepcopy(event["content_block"])
            elif kind == "content_block_delta":
                index, delta = event["index"], event["delta"]
                if index not in self.open_blocks:
                    raise InvalidResponseError("Content delta outside an open block")
                block = self.blocks[index]
                if delta["type"] == "input_json_delta":
                    self.arguments[index] = self.arguments.get(index, "") + delta["partial_json"]
                else:
                    field = {
                        "text_delta": "text",
                        "thinking_delta": "thinking",
                        "signature_delta": "signature",
                    }.get(delta["type"])
                    if field is None:
                        raise InvalidResponseError("Unsupported streamed content delta")
                    block[field] = block.get(field, "") + delta[field]
            elif kind == "content_block_stop":
                index = event["index"]
                if index not in self.open_blocks:
                    raise InvalidResponseError("Unexpected content block stop")
                self.open_blocks.remove(index)
                # The finish reason arrives later. Defer JSON decoding so a
                # token-limited tool block can be discarded instead of raising here.
            elif kind == "message_delta":
                self.data.update(event["delta"])
                self.data.setdefault("usage", {}).update(event.get("usage", {}))
            elif kind == "message_stop":
                self.done = True
        else:
            for key, value in event.items():
                if key != "candidates":
                    self.data[key] = value
            candidates = event.get("candidates", [])
            if candidates:
                if len(candidates) != 1 or candidates[0].get("index", 0) != 0:
                    raise InvalidResponseError("Expected one streamed candidate")
                candidate = candidates[0]
                target = self.data.setdefault(
                    "candidates", [{"content": {"role": "model", "parts": []}}]
                )[0]
                target["content"]["parts"].extend(candidate.get("content", {}).get("parts", []))
                target.update({k: v for k, v in candidate.items() if k != "content"})

    def finish(self) -> dict:
        # Accept a final SSE record without a trailing blank line.
        self.feed("")
        if self.api_format == "gemini":
            self.done = bool((self.data.get("promptFeedback") or {}).get("blockReason")) or bool(
                (self.data.get("candidates") or [{}])[0].get("finishReason")
            )
        if not self.done:
            raise InvalidResponseError("Model stream ended before completion; not retried")
        if self.api_format == "chat_completions":
            choice = (self.data.get("choices") or [{}])[0]
            if not choice.get("finish_reason"):
                raise InvalidResponseError("Stream is missing its finish reason")
            if self.calls:
                choice["message"]["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        elif self.api_format == "anthropic":
            limited = self.data.get("stop_reason") == "max_tokens"
            if not self.data.get("stop_reason") or (
                self.open_blocks
                and not (
                    limited and all(self.blocks[i]["type"] == "tool_use" for i in self.open_blocks)
                )
            ):
                raise InvalidResponseError("Stream contains unfinished content")
            for index, arguments in self.arguments.items():
                if self.blocks[index]["type"] != "tool_use":
                    raise InvalidResponseError("Tool arguments outside a tool block")
                try:
                    self.blocks[index]["input"] = arguments if limited else json.loads(arguments)
                except ValueError:
                    raise InvalidResponseError("Invalid provider tool arguments") from None
            self.data["content"] = [self.blocks[i] for i in sorted(self.blocks)]
        return self.data
