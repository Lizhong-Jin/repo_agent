"""Verify actual host registration and round-trip through the agent loop."""

import json

import httpx
import pytest

import cli.main as cli
from llm import LLMClient
from tools._internal.web_backend import BraveSearchAdapter, WebBackend


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_registers_configured_host_search_and_returns_results(tmp_path, monkeypatch, enabled):
    rounds = []
    search_requests = []

    def search(request):
        search_requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(
                json.dumps(
                    {
                        "type": "search",
                        "web": {
                            "results": [
                                {
                                    "title": "Official docs",
                                    "url": "https://docs.python.org/3/",
                                    "description": "Search snippet",
                                }
                            ]
                        },
                    }
                ).encode()
            ),
        )

    original_adapter = BraveSearchAdapter
    monkeypatch.setattr(
        "tools._internal.web_backend.BraveSearchAdapter",
        lambda key: original_adapter(
            key,
            transport_factory=lambda: httpx.MockTransport(search),
        ),
    )
    closed = []
    original_close = WebBackend.close

    def close(backend):
        closed.append(True)
        original_close(backend)

    monkeypatch.setattr(WebBackend, "close", close)

    def model(request):
        body = json.loads(request.content)
        rounds.append(body)
        names = {item["function"]["name"] for item in body["tools"]}
        assert ("web_search" in names) is enabled
        if enabled and len(rounds) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "search-call",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"queries": ["Python docs"]}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            if enabled:
                observation = json.loads(body["messages"][-1]["content"])
                assert observation["data"]["results"][0]["items"][0]["source"] == "docs.python.org"
                assert observation["data"]["untrusted"]
            message = {"role": "assistant", "content": "已完成。"}
            finish = "stop"
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    (tmp_path / ".env").write_text(
        "AGENT_WEB_SEARCH_PROVIDER="
        + ("brave" if enabled else "off")
        + "\nBRAVE_SEARCH_API_KEY=private-search-key\n"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "model-key")
    monkeypatch.setenv("LLM_STREAM", "false")
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent",
            "查找 Python 文档",
            "--sandbox",
            "local",
            "--root",
            str(tmp_path),
            "--model",
            "mock",
            "--context-window",
            "32000",
        ],
    )
    with httpx.Client(transport=httpx.MockTransport(model)) as http:
        monkeypatch.setattr("cli.runtime_setup.LLMClient", lambda config: LLMClient(config, http_client=http))
        cli.main()
    assert len(search_requests) == int(enabled)
    assert len(closed) == int(enabled)
    assert "private-search-key" not in json.dumps(rounds)
    for logfile in (tmp_path / "logs").rglob("*.json*"):
        assert "private-search-key" not in logfile.read_text()
