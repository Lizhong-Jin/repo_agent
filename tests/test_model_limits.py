import httpx
import pytest

from agent.Tracing import ModelCallRecord, RunStats
from cli.live import SessionStatus
from llm import ConfigurationError, LLMClient, LLMConfig, Usage
from llm.model_limits import ModelContextLimit


@pytest.mark.parametrize(
    "provider,model,path,payload,expected,header",
    [
        ("gemini", "models/gemini-test", "/v1/models/gemini-test",
         {"name": "models/gemini-test", "inputTokenLimit": 1000, "outputTokenLimit": 100},
         ModelContextLimit(1000, "input"), "x-goog-api-key"),
        ("anthropic", "claude-latest", "/v1/models/claude-latest",
         {"id": "claude-dated", "max_input_tokens": 2000, "max_tokens": 200},
         ModelContextLimit(2000, "input"), "x-api-key"),
        ("deepseek", "m", "/v1/models",
         {"data": [{"id": "other", "context_length": 9000},
                   {"id": "m", "context_length": 3000}]},
         ModelContextLimit(3000, "context"), "authorization"),
        ("qwen", "org/m", "/v1/models",
         {"data": [{"id": "org/m", "max_model_len": 4000}]},
         ModelContextLimit(4000, "context"), "authorization"),
    ],
)
def test_provider_metadata_authentication_paths_and_cache(
    provider, model, path, payload, expected, header
):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and not request.content
        assert request.url.host == "gateway.test" and request.url.path == path
        assert request.headers[header].endswith("private-key")
        assert request.extensions["timeout"]["read"] == 2.0
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(
            LLMConfig(provider, model, api_key="private-key", base_url="https://gateway.test/v1/"),
            http_client=http,
        )
        assert client.get_context_limit() == expected
        assert client.get_context_limit() == expected
        assert len(requests) == 1
        assert client.get_context_limit(refresh=True) == expected
        assert len(requests) == 2


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.5, "8192", [], {}])
def test_invalid_limit_is_unknown(value):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"data": [{"id": "m", "context_length": value, "max_tokens": 4000}]}
    ))) as http:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http)
        assert client.get_context_limit() is None


@pytest.mark.parametrize("payload", [
    {}, [], {"data": None}, {"data": [None]},
    {"data": [{"id": "other", "context_length": 1000}]},
    {"data": [{"id": "m", "max_tokens": 1000}]},
    {"data": [{"id": "m", "context_length": 1000}] * 2},
    {"error": {"message": "private"}, "data": [{"id": "m", "context_length": 1000}]},
])
def test_missing_ambiguous_or_output_only_metadata_is_unknown(payload):
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)
    )) as http:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http)
        assert client.get_context_limit() is None


@pytest.mark.parametrize(
    "failure", ["timeout", "network", "json", "large", 401, 404, 429, 500, 302]
)
def test_optional_lookup_fails_closed_without_retry_or_redirect(failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-key")
        if failure == "network":
            raise httpx.ConnectError("private-key")
        if failure == "json":
            return httpx.Response(200, content="not JSON")
        if failure == "large":
            return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))
        return httpx.Response(failure, headers={"location": "https://other.test/models"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        client = LLMClient(LLMConfig("deepseek", "m", api_key="key"), http_client=http)
        assert client.get_context_limit() is None
        assert client.get_context_limit() is None
        assert len(requests) == 1


def test_unknown_cache_can_be_refreshed_and_clients_do_not_share_limits():
    results = iter([{}, {"data": [{"id": "m", "context_length": 2000}]}, {}])
    with httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=next(results))
    )) as http:
        config = LLMConfig("deepseek", "m", api_key="key")
        client = LLMClient(config, http_client=http)
        assert client.get_context_limit() is None
        assert client.get_context_limit(refresh=True).tokens == 2000
        assert LLMClient(config, http_client=http).get_context_limit() is None


class MetadataClient:
    def __init__(self, limit):
        self.limit = limit
        self.lookups = []

    def get_context_limit(self, *, refresh=False):
        self.lookups.append(refresh)
        return self.limit


def test_manual_override_auto_refresh_and_new_model_reset(tmp_path):
    client = MetadataClient(ModelContextLimit(2000, "context"))
    status = SessionStatus(tmp_path, context_window=1000)
    status.bind_context_model(client)
    assert status.context_window == 1000 and not client.lookups
    assert "手动设置" in status.describe_context()
    assert "服务端自动获取" in status.context_command("/context auto")
    assert status.context_window == 2000 and client.lookups == [True]
    status.context_command("/context 500")
    assert status.context_window == 500
    status.bind_context_model(MetadataClient(None), reset=True)
    assert status.context_window is None
    assert "自动获取未返回上限" in status.describe_context()
    assert "%" not in status.describe_context()


def test_input_cap_uses_input_usage_and_reset_clears_it(tmp_path):
    status = SessionStatus(tmp_path)
    status.bind_context_model(MetadataClient(ModelContextLimit(1000, "input")))
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(200, 100)))
    status("model_end", stats)
    assert "输入上下文" in status.describe_context()
    assert "20.0%" in status.describe_context()
    assert status.context_tokens == 300
    assert status.totals == {"input_tokens": 200, "output_tokens": 100}
    status.context_command("/context 1000")
    assert "30.0%" in status.describe_context()
    status.context_command("/context auto")
    stats.model_calls.append(ModelCallRecord(2, usage=Usage(400, None)))
    status("model_end", stats)
    assert "40.0%" in status.describe_context()
    stats.model_calls.append(ModelCallRecord(3, usage=Usage(None, 100)))
    status("model_end", stats)
    assert "%" not in status.describe_context()
    status.reset_context()
    assert status.context_input_tokens is None
    assert status.context_window == 1000


def test_invalid_command_keeps_existing_limit(tmp_path):
    status = SessionStatus(tmp_path, context_window=1000)
    for command in ("/context wat", "/context auto 10", "/context -1"):
        with pytest.raises(ConfigurationError):
            status.context_command(command)
        assert status.context_window == 1000
