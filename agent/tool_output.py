"""Deterministic model-facing output limits; never modify durable tool receipts."""

import json
from copy import deepcopy
from dataclasses import replace

MIN_CALL_CHARS = 1024
DEFAULT_ROUND_CHARS = 128 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def validate_output_limit(value):
    if type(value) is not int or value < MIN_CALL_CHARS:
        raise ValueError(f"max_tool_output_chars must be an integer >= {MIN_CALL_CHARS}")
    return value


class ToolOutputBudget:
    """Reserve shares in input order before dispatch; completion order is irrelevant.

    Limit counts serialized Message.content characters, including JSON metadata;
    provider framing and the assistant's tool arguments are outside this budget.
    """

    def __init__(self, calls, max_chars):
        validate_output_limit(max_chars)
        if not calls or len(calls) > max_chars // MIN_CALL_CHARS:
            raise ValueError("Too many tool calls for the round output budget")
        share, remainder = divmod(max_chars, len(calls))
        self.quotas = {call.id: share + (i < remainder) for i, call in enumerate(calls)}

    def project(self, call, observation):
        quota = self.quotas[call.id]
        if len(observation.content) <= quota:
            return observation
        payload = json.loads(observation.content)
        if call.name == "read_file":
            projected = self._read_page(payload, quota)
            if projected is not None:
                return replace(observation, content=encode(projected))
        # Do not recursively slice arbitrary JSON: array counts, hashes, offsets
        # and commit reports can become false. Omit a whole oversized payload.
        summary = {
            "success": payload["success"],
            "data": {},
            "output_truncated": True,
            "truncation_reason": "round_output_budget",
            "original_chars": len(observation.content),
            "notice": "Result body omitted. Execution status is unchanged. Request smaller "
            "read ranges or narrower searches. Do not repeat writes or commands "
            "just to recover output; inspect current state or the execution ledger.",
        }
        if not payload["success"]:
            error = payload.get("error", {})
            summary["error"] = {
                "code": str(error.get("code", "TOOL_ERROR"))[:80],
                "message": "Error details omitted by output budget.",
            }
        # Preserve small execution/cleanup flags when they fit. Missing fields
        # are intentionally unknown; the full receipt retains all side effects.
        for key in ("exit_code", "timed_out", "cleanup_status", "execution_allowed"):
            if key in payload.get("data", {}):
                candidate = deepcopy(summary)
                candidate["data"][key] = payload["data"][key]
                if len(encode(candidate)) <= quota:
                    summary = candidate
        content = encode(summary)
        # Escape-heavy untrusted error codes must not overrun a small quota.
        if len(content) > quota:
            summary["error"]["code"] = "ERROR_DETAILS_OMITTED"
            content = encode(summary)
        assert len(content) <= quota
        return replace(observation, content=content)

    @staticmethod
    def _read_page(payload, quota):
        """Retain complete numbered lines and correct per-file continuation."""
        results = payload.get("data", {}).get("results")
        if not isinstance(results, list):
            return None
        page = deepcopy(payload)
        page.update(
            output_truncated=True,
            truncation_reason="round_output_budget",
            output_notice="Only complete lines fit. If no line fits, narrow the request or "
            "increase max_tool_output_chars; an identical retry cannot advance.",
        )
        sources = []
        for entry in page["data"]["results"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("data", {}), dict):
                return None
            data = entry.get("data", {})
            content = data.get("content")
            if not isinstance(content, str) or not content:
                continue
            start = data.get("start_line")
            if type(start) is not int:
                return None
            sources.append((data, deepcopy(data), content.split("\n")))
            data.update(content="", end_line=start - 1, next_start_line=start, truncated=True)
        page["data"]["total_output_chars"] = 0
        if len(encode(page)) > quota:
            return None
        total = 0
        for data, original, lines in sources:

            def prefix(count, data=data, original=original, lines=lines, total=total):
                data.update(original)
                if count < len(lines):
                    data.update(
                        content="\n".join(lines[:count]),
                        end_line=original["start_line"] + count - 1,
                        next_start_line=original["start_line"] + count,
                        truncated=True,
                    )
                page["data"]["total_output_chars"] = total + len(data["content"])

            low, high = 0, len(lines)
            while low < high:
                middle = (low + high + 1) // 2
                prefix(middle)
                if len(encode(page)) <= quota:
                    low = middle
                else:
                    high = middle - 1
            prefix(low)
            total += len(data["content"])
        return page
