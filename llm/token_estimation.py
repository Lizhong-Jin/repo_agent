"""Offline display estimates, not model tokenization or billable usage."""

import json
import math
from dataclasses import asdict


def estimate_context_tokens(messages, tools=()):
    # Count portable content once. Provider state may duplicate the text/tool
    # calls or contain encrypted reasoning whose token length is unknowable.
    content = {
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "tool_calls": [asdict(call) for call in message.tool_calls],
                "name": message.name,
                "tool_call_id": message.tool_call_id,
            }
            for message in messages
        ],
        "tools": [asdict(tool) for tool in tools],
    }
    text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    # A language-aware heuristic keeps Chinese prompts from being treated as
    # English text. Neither this ratio nor JSON framing is an exact tokenizer.
    ascii_chars = sum(char.isascii() for char in text)
    return math.ceil(ascii_chars / 4 + (len(text) - ascii_chars) * 1.5)
