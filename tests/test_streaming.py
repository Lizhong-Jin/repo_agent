import asyncio
import json

import httpx
import pytest

from llm import (
    AsyncLLMClient,
    ConfigurationError,
    InvalidResponseError,
    LLMClient,
    LLMConfig,
    LLMRequest,
    LLMTimeoutError,
    Message,
    ProviderError,
)

REQUEST = LLMRequest([Message("user", "hello")])


def sse(events):
    return (
        ": heartbeat\r\n\r\n"
        + "".join(
            "data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)) + "\r\n\r\n"
            for e in events
        )
    ).encode()


def chat(delta, finish=None):
    return {
        "id": "r1",
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


CHAT = [
    chat({"role": "assistant", "reasoning_content": "思"}),
    chat({"reasoning_content": "考", "content": "你好"}),
    chat(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "a",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }
            ]
        }
    ),
    chat(
        {
            "tool_calls": [
                {
                    "index": 1,
                    "id": "b",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"b"}'},
                }
            ]
        }
    ),
    chat({"tool_calls": [{"index": 0, "function": {"arguments": '"中文"}'}}]}, "tool_calls"),
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
    "[DONE]",
]
ANTHROPIC = [
    {
        "type": "message_start",
        "message": {"id": "r1", "role": "assistant", "content": [], "usage": {"input_tokens": 10}},
    },
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "thinking", "thinking": ""},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "思考"},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "signature_delta", "signature": "opaque"},
    },
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "a", "name": "read_file", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '{"path":'},
    },
    {
        "type": "content_block_delta",
        "index": 1,
        "delta": {"type": "input_json_delta", "partial_json": '"中文"}'},
    },
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}},
    {"type": "message_stop"},
]
RESPONSES = [
    {"type": "response.output_text.delta", "delta": "你好"},
    {
        "type": "response.completed",
        "response": {
            "id": "r1",
            "status": "completed",
            "output": [
                {"type": "reasoning", "encrypted_content": "opaque"},
                {
                    "type": "function_call",
                    "call_id": "a",
                    "name": "read_file",
                    "arguments": '{"path":"中文"}',
                },
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    },
]
GEMINI = [
    {"candidates": [{"content": {"role": "model", "parts": [{"text": "你好"}]}}]},
    {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "id": "a",
                                "name": "read_file",
                                "args": {"path": "中文"},
                            },
                            "thoughtSignature": "opaque",
                        }
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
    },
]


class Pieces(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, content, failure=None):
        self.content = content
        self.failure = failure
        self.closed = False

    def __iter__(self):
        for i in range(0, len(self.content), 7):
            yield self.content[i : i + 7]
        if self.failure:
            raise self.failure

    async def __aiter__(self):
        for piece in self:
            yield piece

    def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "provider,events",
    [("deepseek", CHAT), ("anthropic", ANTHROPIC), ("openai", RESPONSES), ("gemini", GEMINI)],
)
def test_stream_assembly_and_state_roundtrip(provider, events, asynchronous):
    source = Pieces(sse(events))

    def handler(request):
        body = json.loads(request.content)
        if provider == "gemini":
            assert request.url.path.endswith(":streamGenerateContent")
            assert request.url.query == b"alt=sse"
            assert "stream" not in body
        else:
            assert body["stream"] is True
        assert request.extensions["timeout"] == {
            "connect": 10,
            "read": 300,
            "write": 30,
            "pool": 10,
        }
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    transport = httpx.MockTransport(handler)
    config = LLMConfig(provider, "m", api_key="key")
    if asynchronous:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                client = AsyncLLMClient(config, http_client=http)
                return await client.generate(REQUEST), client.adapter

        response, adapter = asyncio.run(run())
    else:
        with httpx.Client(transport=transport) as http:
            client = LLMClient(config, http_client=http)
            response, adapter = client.generate(REQUEST), client.adapter
    assert source.closed
    assert response.tool_calls[0].arguments == {"path": "中文"}
    assert response.usage.total_tokens == 30
    assert response.finish_reason == "tool_calls"
    history = [*REQUEST.messages, response.to_message()]
    history.extend(Message.tool_result(c, "ok") for c in response.tool_calls)
    encoded = adapter.encode(LLMRequest(history))
    wire = json.dumps(encoded, ensure_ascii=False)
    assert "opaque" in wire if provider != "deepseek" else "思考" in wire
    if provider == "deepseek":
        assert len(response.tool_calls) == 2
        assert response.text == "你好"


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "events,failure,error",
    [
        (CHAT[:-1], None, InvalidResponseError),
        ([chat({"content": "partial"}), "[DONE]"], None, InvalidResponseError),
        ([chat({"content": "partial"})], httpx.ReadTimeout("private"), LLMTimeoutError),
        ([{"type": "error", "error": {"message": "private"}}], None, ProviderError),
        (["{bad"], None, InvalidResponseError),
        (ANTHROPIC[:-1], None, InvalidResponseError),
    ],
)
def test_broken_stream_closes_without_retry(events, failure, error, asynchronous):
    seen = []
    source = Pieces(sse(events), failure)

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)

    config = LLMConfig("deepseek", "m", api_key="key")
    transport = httpx.MockTransport(handler)
    with pytest.raises(error) as caught:
        if asynchronous:

            async def run():
                async with httpx.AsyncClient(transport=transport) as http:
                    return await AsyncLLMClient(config, http_client=http).generate(REQUEST)

            asyncio.run(run())
        else:
            with httpx.Client(transport=transport) as http:
                LLMClient(config, http_client=http).generate(REQUEST)
    assert source.closed
    assert len(seen) == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("field", ["timeout", "connect_timeout", "write_timeout", "pool_timeout"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10"])
def test_invalid_timeout(field, value):
    with pytest.raises(ConfigurationError):
        LLMConfig("deepseek", "m", **{field: value})


@pytest.mark.parametrize("provider", ["deepseek", "gemini"])
def test_nonstream_opt_out(provider):
    def handler(request):
        if provider == "gemini":
            assert request.url.path.endswith(":generateContent")
            assert not request.url.query
            data = {
                "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]
            }
        else:
            assert json.loads(request.content)["stream"] is False
            data = {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ]
            }
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = LLMClient(
            LLMConfig(provider, "m", api_key="key", stream=False), http_client=http
        ).generate(REQUEST)
    assert result.text == "ok"


@pytest.mark.parametrize(
    "events",
    [
        [e for e in ANTHROPIC if e["type"] != "content_block_stop"],
        ANTHROPIC[:-1],
    ],
)
def test_anthropic_requires_complete_blocks_and_message(events):
    from llm.streaming import StreamAssembler

    assembler = StreamAssembler("anthropic")
    for line in sse(events).decode().splitlines():
        assembler.feed(line)
    with pytest.raises(InvalidResponseError):
        assembler.finish()
