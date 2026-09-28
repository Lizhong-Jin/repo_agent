"""Host-only web_fetch registration, including native failure isolation."""

import json
from types import SimpleNamespace

import httpx
import pytest

import cli.main as cli
from llm import LLMClient


@pytest.mark.parametrize("mode", ["local", "native"])
def test_cli_fetch_and_cached_read_without_search_key(tmp_path, monkeypatch, mode):
    requested = []

    async def download(self, url):
        requested.append(url)
        return {
            "body": b"first\nsecond\nthird",
            "content_type": "text/plain",
            "content_type_header": "text/plain",
            "final_url": url,
        }

    monkeypatch.setattr("tools._internal.web_pages.PublicHTTP.download", download)
    native_closed = []
    native = SimpleNamespace(
        healthy=True,
        tools=lambda: [],
        close=lambda: native_closed.append(True),
        execution_context=lambda: {"gpu_access": {"enabled": False}},
    )
    monkeypatch.setattr("cli.execution_environment.NativeBackend", lambda *args, **kwargs: native)
    rounds = []

    def model(request):
        body = json.loads(request.content)
        rounds.append(body)
        names = {item["function"]["name"] for item in body["tools"]}
        assert "web_fetch" in names and "web_search" not in names
        if len(rounds) == 1:
            arguments = {"targets": [{"url": "https://docs.example.org", "line_count": 1}]}
        elif len(rounds) == 2:
            result = json.loads(body["messages"][-1]["content"])
            page = result["data"]["results"][0]
            assert page["content"] == "first" and result["data"]["untrusted"]
            arguments = {
                "targets": [
                    {"ref_id": page["ref_id"], "start_line": 2},
                    {"ref_id": "doc_" + "0" * 32},
                ]
            }
        else:
            result = json.loads(body["messages"][-1]["content"])
            assert result["data"]["results"][0]["content"] == "second\nthird"
            assert result["data"]["results"][1]["error"]["code"] == "REF_NOT_FOUND"
            assert native.healthy
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "已读取文档。"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"fetch-{len(rounds)}",
                                    "type": "function",
                                    "function": {
                                        "name": "web_fetch",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    (tmp_path / ".env").write_text("AGENT_WEB_FETCH_ENABLED=true\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key")
    monkeypatch.setenv("LLM_STREAM", "false")
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent",
            "读取文档",
            "--sandbox",
            mode,
            "--root",
            str(tmp_path),
            "--model",
            "mock",
            "--context-window",
            "32000",
        ],
    )
    with httpx.Client(transport=httpx.MockTransport(model)) as http:
        monkeypatch.setattr(
            "cli.runtime_setup.LLMClient", lambda config: LLMClient(config, http_client=http)
        )
        cli.main()
    assert requested == ["https://docs.example.org"]
    assert native.healthy and len(native_closed) == int(mode == "native")
