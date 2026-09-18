"""Offline contract tests: no model account, network access or inference charges."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from llm import (
    PROVIDERS,
    AsyncLLMClient,
    AuthenticationError,
    ConfigurationError,
    InvalidRequestError,
    InvalidResponseError,
    LLMClient,
    LLMConfig,
    LLMConnectionError,
    LLMRequest,
    LLMTimeoutError,
    Message,
    ProviderError,
    RateLimitError,
    ToolCall,
    ToolDefinition,
)
from llm.adapters import ADAPTERS
from llm.providers import get_provider

TOOL = ToolDefinition(
    "read_file",
    "Read a file",
    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
)
REQUEST = LLMRequest([Message("system", "你是代码助手"), Message("user", "检查代码")], tools=[TOOL])


def payload(api_format, *, calls=True):
    if api_format == "chat_completions":
        message = {"role": "assistant", "content": "检查完成", "reasoning_content": "state"}
        if calls:
            message["tool_calls"] = [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"主程序.py"}'},
                }
            ]
        return {
            "id": "chat_1",
            "model": "resolved-model",
            "choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        }
    if api_format == "responses":
        output = [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}]
        if calls:
            output.append(
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"主程序.py"}',
                    "status": "completed",
                }
            )
        else:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg_1",
                    "status": "completed",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "检查完成", "annotations": []}],
                }
            )
        return {
            "id": "resp_1",
            "model": "resolved-model",
            "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": 20,
                "output_tokens": 5,
                "total_tokens": 25,
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    if api_format == "anthropic":
        blocks = [
            {"type": "thinking", "thinking": "state", "signature": "signed"},
            {"type": "redacted_thinking", "data": "encrypted"},
            {"type": "text", "text": "检查完成"},
        ]
        if calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read_file",
                    "input": {"path": "主程序.py"},
                }
            )
        return {
            "id": "msg_1",
            "model": "resolved-model",
            "role": "assistant",
            "content": blocks,
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 8,
                "cache_creation_input_tokens": 2,
            },
        }
    parts = [{"text": "internal", "thought": True}, {"text": "检查完成"}]
    if calls:
        parts.append(
            {
                "functionCall": {"name": "read_file", "args": {"path": "主程序.py"}},
                "thoughtSignature": "signed",
            }
        )
    return {
        "responseId": "gemini_1",
        "modelVersion": "resolved-model",
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": 20,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 2,
            "totalTokenCount": 25,
        },
    }


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_http_and_tool_round_trip(provider):
    preset = get_provider(provider)
    first_payload = payload(preset.api_format)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        assert str(request.url).startswith(preset.base_url)
        auth = {"anthropic": "x-api-key", "gemini": "x-goog-api-key"}.get(
            preset.api_format, "authorization"
        )
        assert "test-key" in request.headers[auth]
        assert "test-key" not in str(request.url)
        return httpx.Response(
            200, json=first_payload if len(sent) == 1 else payload(preset.api_format, calls=False)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(
            LLMConfig(provider, "test-model", api_key="test-key"), http_client=transport
        ) as client:
            first = client.generate(REQUEST)
            assert first.finish_reason == "tool_calls"
            assert first.tool_calls[0].arguments == {"path": "主程序.py"}
            assert first.usage.input_tokens == 20
            assert first.usage.output_tokens == 5
            assert first.usage.total_tokens == 25
            restored = Message.from_dict(json.loads(json.dumps(first.to_message().to_dict())))
            followup = LLMRequest(
                [
                    *REQUEST.messages,
                    restored,
                    Message.tool_result(first.tool_calls[0], {"content": "print(1)"}),
                ],
                tools=[TOOL],
            )
            second = client.generate(followup)
            assert second.text == "检查完成"
            assert second.finish_reason == "stop"
        assert not transport.is_closed  # The caller owns an injected HTTP client.
    if preset.api_format == "chat_completions":
        assert sent[1]["messages"][-2] == first_payload["choices"][0]["message"]
        assert sent[1]["messages"][-1]["tool_call_id"] == first.tool_calls[0].id
        if provider == "minimax":
            assert sent[0]["reasoning_split"] is True
    elif preset.api_format == "responses":
        assert sent[1]["input"][2:-1] == first_payload["output"]
        assert sent[1]["input"][-1]["call_id"] == "call_1"  # Not the fc_1 item ID.
        assert sent[0]["store"] is False
        assert sent[0]["tools"][0]["strict"] is False
    elif preset.api_format == "anthropic":
        assert sent[1]["messages"][-2]["content"] == first_payload["content"]
        assert sent[0]["system"] == "你是代码助手"
    else:
        assert sent[1]["contents"][-2] == first_payload["candidates"][0]["content"]
        assert "id" not in sent[1]["contents"][-1]["parts"][0]["functionResponse"]


@pytest.mark.parametrize("provider", ["deepseek", "anthropic", "gemini", "openai"])
def test_parallel_calls_and_error_results(provider):
    preset = get_provider(provider)
    adapter = ADAPTERS[preset.api_format](provider, "m")
    calls = [ToolCall("a", "read_file", {"path": "a"}), ToolCall("b", "read_file", {"path": "b"})]
    request = LLMRequest(
        [
            Message("user", "read"),
            Message("assistant", tool_calls=calls),
            Message.tool_result(calls[0], "missing", is_error=True),
            Message.tool_result(calls[1], "ok"),
        ],
        tools=[TOOL],
    )
    body = adapter.encode(request)
    if provider == "anthropic":
        assert len(body["messages"][-1]["content"]) == 2
        assert body["messages"][-1]["content"][0]["is_error"] is True
    elif provider == "gemini":
        assert len(body["contents"][-1]["parts"]) == 2
        assert body["contents"][-1]["parts"][0]["functionResponse"]["response"] == {
            "error": "missing"
        }
    else:
        assert "error" in json.dumps(body)


def test_gemini_native_id_and_signature_preserved():
    adapter = ADAPTERS["gemini"]("gemini", "m")
    data = payload("gemini")
    data["candidates"][0]["content"]["parts"][-1]["functionCall"]["id"] = "native-id"
    result = adapter.decode(data)
    body = adapter.encode(
        LLMRequest(
            [
                Message("user", "x"),
                result.to_message(),
                Message.tool_result(result.tool_calls[0], "ok"),
            ]
        )
    )
    assert body["contents"][-1]["parts"][0]["functionResponse"]["id"] == "native-id"
    assert body["contents"][-2]["parts"][-1]["thoughtSignature"] == "signed"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "deepseek"])
def test_tool_choice_and_generation_options(provider):
    preset = get_provider(provider)
    adapter = ADAPTERS[preset.api_format](provider, "m")
    body = adapter.encode(
        replace(REQUEST, temperature=0.5, tool_choice="required", max_output_tokens=100)
    )
    if provider == "gemini":
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
        assert body["generationConfig"]["maxOutputTokens"] == 100
        assert (
            body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == TOOL.parameters
        )
    else:
        assert body["temperature"] == 0.5
        assert body["tool_choice"] == ({"type": "any"} if provider == "anthropic" else "required")


def test_zhipu_unsupported_choice_fails_locally():
    with pytest.raises(InvalidRequestError):
        ADAPTERS["chat_completions"]("zhipu", "m").encode(replace(REQUEST, tool_choice="required"))


def test_extra_options_do_not_overwrite_common_config():
    adapter = ADAPTERS["gemini"]("gemini", "m")
    extra = {"generationConfig": {"thinkingConfig": {"thinkingBudget": 1024}}}
    original = deepcopy(extra)
    body = adapter.encode(replace(REQUEST, extra=extra))
    assert body["generationConfig"]["maxOutputTokens"] == 4096
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024
    assert extra == original
    for invalid in (
        {"stream": True},
        {"model": "other"},
        {"store": True},
        {"n": 2},
        {"generationConfig": {"candidateCount": 2}},
    ):
        with pytest.raises(InvalidRequestError):
            adapter.encode(replace(REQUEST, extra=invalid))


@pytest.mark.parametrize("target,model", [("deepseek", "other-model"), ("moonshot", "m")])
def test_do_not_forward_state_to_different_model(target, model):
    result = ADAPTERS["chat_completions"]("deepseek", "m").decode(
        payload("chat_completions", calls=False)
    )
    request = LLMRequest([Message("user", "x"), result.to_message(), Message("user", "next")])
    with pytest.raises(InvalidRequestError):
        ADAPTERS["chat_completions"](target, model).encode(request)


def test_changed_assistant_state_and_raw_mutation():
    adapter = ADAPTERS["responses"]("openai", "m")
    result = adapter.decode(payload("responses", calls=False))
    result.raw["output"].clear()
    assert result.message.provider_state.payload
    modified = replace(result.to_message(), content="changed")
    with pytest.raises(InvalidRequestError):
        adapter.encode(LLMRequest([Message("user", "x"), modified, Message("user", "next")]))


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [Message("system", "only system")],
        [Message("assistant", "first")],
        [Message("user", "x"), Message("system", "late")],
        [Message("user", "x"), Message("tool", "x", tool_call_id="missing", name="read_file")],
        [Message("user", "x"), Message("assistant", tool_calls=[ToolCall("a", "read_file", {})])],
    ],
)
def test_invalid_transcripts(messages):
    with pytest.raises(InvalidRequestError):
        LLMRequest(messages)


def test_duplicate_results_and_ids_rejected():
    call = ToolCall("a", "read_file", {})
    history = [
        Message("user", "x"),
        Message("assistant", tool_calls=[call]),
        Message.tool_result(call, "ok"),
    ]
    for tail in ([Message.tool_result(call, "again")], [Message("assistant", tool_calls=[call])]):
        with pytest.raises(InvalidRequestError):
            LLMRequest(history + tail)


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"choices": []},
        {"choices": [{"message": {"role": "assistant", "content": 12}}]},
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "a",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{broken"},
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "a",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "[]"},
                            }
                        ],
                    }
                }
            ]
        },
    ],
)
def test_malformed_provider_data_is_not_retried(body):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with LLMClient(
            LLMConfig("deepseek", "m", api_key="secret"), http_client=transport
        ) as client:
            with pytest.raises(InvalidResponseError):
                client.generate(REQUEST)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "status,error",
    [
        (400, InvalidRequestError),
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, InvalidRequestError),
        (429, RateLimitError),
        (500, ProviderError),
        (529, ProviderError),
        (302, ProviderError),
    ],
)
def test_error_mapping_and_retry_limit(status, error):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status,
            headers={"retry-after": "0", "x-request-id": "req-1"},
            json={"error": {"message": "secret-key private-prompt"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="secret-key"), http_client=transport)
        with pytest.raises(error) as exc:
            client.generate(REQUEST)
        assert exc.value.status_code == status
        assert exc.value.request_id == "req-1"
        assert "secret-key" not in str(exc.value)
        assert "private-prompt" not in str(exc.value)
    assert len(seen) == (3 if status in {429, 500, 529} else 1)


def test_retry_after_then_success():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=payload("chat_completions", calls=False)),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))) as transport:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=transport)
        with patch("llm.client.time.sleep") as sleep:
            assert client.generate(REQUEST).text == "检查完成"
        sleep.assert_called_once_with(2)


@pytest.mark.parametrize(
    "exception,error",
    [(httpx.ReadTimeout, LLMTimeoutError), (httpx.ConnectError, LLMConnectionError)],
)
def test_transport_failures_not_retried(exception, error):
    seen = []

    def handler(request):
        seen.append(request)
        raise exception("sensitive details", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=transport)
        with pytest.raises(error) as exc:
            client.generate(REQUEST)
        assert "sensitive details" not in str(exc.value)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "failure"}},
        {"base_resp": {"status_code": 1004, "status_msg": "failure"}},
    ],
)
def test_http_200_application_error(body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as transport:
        with pytest.raises(ProviderError):
            LLMClient(LLMConfig("minimax", "m", api_key="key"), http_client=transport).generate(
                REQUEST
            )


def test_finish_reason_precedence_and_missing_usage():
    adapter = ADAPTERS["chat_completions"]("deepseek", "m")
    data = payload("chat_completions")
    data.pop("usage")
    data["choices"][0]["finish_reason"] = "length"
    result = adapter.decode(data)
    assert result.finish_reason == "length"  # Never execute a truncated call as a normal tool turn.
    assert result.usage.input_tokens is None
    assert result.usage.total_tokens is None
    data["choices"][0]["finish_reason"] = "unknown-new-reason"
    assert adapter.decode(data).finish_reason == "other"
    assert adapter.decode(data).provider_finish_reason == "unknown-new-reason"


def test_blocked_and_incomplete_responses():
    google = ADAPTERS["gemini"]("gemini", "m")
    assert google.decode({"promptFeedback": {"blockReason": "SAFETY"}}).finish_reason == "blocked"
    openai = ADAPTERS["responses"]("openai", "m")
    data = payload("responses", calls=False)
    data.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    assert openai.decode(data).finish_reason == "length"
    data.update(status="completed")
    data["output"][-1]["content"] = [{"type": "refusal", "refusal": "cannot comply"}]
    assert openai.decode(data).finish_reason == "blocked"


def test_config_environment_aliases_and_secret_repr(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    with LLMClient(LLMConfig("chatgpt", "m")) as client:
        assert client.provider.name == "openai"
    assert client._http.is_closed
    assert "my-secret" not in repr(LLMConfig("claude", "m", api_key="my-secret"))
    assert get_provider("kimi").name == "moonshot"
    assert get_provider("glm").name == "zhipu"
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        LLMClient(LLMConfig("openai", "m"))
    for url in (
        "",
        "example.com",
        "ftp://example.com",
        "https://key@example.com",
        "https://x?key=y",
    ):
        with pytest.raises(ConfigurationError):
            LLMClient(LLMConfig("openai", "m", api_key="key", base_url=url))


@pytest.mark.parametrize("provider", ["deepseek", "openai", "anthropic", "gemini"])
def test_async_same_contract_and_retry(provider):
    async def run():
        seen = []

        async def handler(request):
            seen.append(json.loads(request.content))
            if len(seen) == 1:
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, json=payload(get_provider(provider).api_format, calls=False))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            async with AsyncLLMClient(
                LLMConfig(provider, "m", api_key="key"), http_client=transport
            ) as client:
                assert (await client.generate(REQUEST)).text == "检查完成"
            assert not transport.is_closed
        assert len(seen) == 2
        assert seen[0] == seen[1]

    asyncio.run(run())


def test_async_cancellation_propagates():
    async def run():
        async def handler(request):
            raise asyncio.CancelledError

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client = AsyncLLMClient(
                LLMConfig("deepseek", "m", api_key="key"), http_client=transport
            )
            with pytest.raises(asyncio.CancelledError):
                await client.generate(REQUEST)

    asyncio.run(run())


def test_parallel_result_order_is_portable():
    a, b = ToolCall("a", "read_file", {}), ToolCall("b", "read_file", {})
    with pytest.raises(InvalidRequestError, match="original call order"):
        LLMRequest(
            [
                Message("user", "x"),
                Message("assistant", tool_calls=[a, b]),
                Message.tool_result(b, "b"),
                Message.tool_result(a, "a"),
            ]
        )


@pytest.mark.parametrize("case", ["empty", "missing_calls", "duplicate_ids", "nan"])
def test_invalid_success_responses_fail_closed(case):
    data = payload("chat_completions")
    message = data["choices"][0]["message"]
    if case == "empty":
        message.update(content=None, tool_calls=[])
        data["choices"][0]["finish_reason"] = "stop"
    elif case == "missing_calls":
        message["tool_calls"] = []
    elif case == "duplicate_ids":
        message["tool_calls"] *= 2
    else:
        message["tool_calls"][0]["function"]["arguments"] = '{"path": NaN}'
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as transport:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=transport)
        with pytest.raises(InvalidResponseError):
            client.generate(REQUEST)


def test_invalid_extra_include_is_a_request_error():
    adapter = ADAPTERS["responses"]("openai", "m")
    with pytest.raises(InvalidRequestError):
        adapter.encode(replace(REQUEST, extra={"include": "wrong type"}))


def test_non_json_tool_arguments_rejected_locally():
    with pytest.raises(InvalidRequestError):
        ToolCall("a", "read_file", {"path": float("nan")})
