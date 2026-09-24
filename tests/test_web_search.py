"""Search boundary tests use simulated provider HTTP only; no keys or network needed."""

import asyncio
import gzip
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from llm import ToolCall
from tools._internal.web_backend import (
    MAX_OUTPUT_CHARS,
    BraveSearchAdapter,
    WebBackend,
)
from tools.factory import create_default_tools
from tools.web_tools import WebSearchTool, create_web_tools


def response(payload=None, *, status=200, headers=None, body=None):
    if body is None:
        body = json.dumps(
            payload
            if payload is not None
            else {
                "type": "search",
                "web": {"results": []},
            }
        ).encode()
    return httpx.Response(
        status,
        headers={"content-type": "application/json", **(headers or {})},
        stream=httpx.ByteStream(body),
    )


def hits(*urls, snippet="A search snippet", title="Documentation"):
    return {
        "type": "search",
        "web": {"results": [{"url": url, "title": title, "description": snippet} for url in urls]},
    }


@pytest.fixture
def make_tool():
    backends = []

    def make(handler, *, timeout=20):
        adapter = BraveSearchAdapter(
            "test-private-key", transport_factory=lambda: httpx.MockTransport(handler)
        )
        backend = WebBackend(adapter, timeout_seconds=timeout)
        backends.append(backend)
        return WebSearchTool(backend)

    yield make
    for backend in backends:
        backend.close()


def test_search_request_and_result_contract(make_tool, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url).startswith(BraveSearchAdapter.endpoint + "?")
        assert request.url.params["q"] == "(asyncio TaskGroup) (site:docs.python.org)"
        assert request.url.params["count"] == "5"
        assert request.url.params["spellcheck"] == "false"
        assert request.headers["X-Subscription-Token"] == "test-private-key"
        assert "cookie" not in request.headers
        return response(
            hits(
                "https://docs.python.org/3/library/asyncio-task.html",
                title="<b>TaskGroup</b> &amp; tasks",
                snippet="A <b>search</b> snippet",
            )
        )

    tool = make_tool(handler)
    result = tool.execute({"queries": ["asyncio TaskGroup"], "domains": ["Docs.Python.org."]})
    assert result.success
    data = result.data
    assert data["provider"] == "brave" and data["untrusted"] is True
    assert data["result_kind"] == "search_snippets"
    entry = data["results"][0]
    assert entry["success"] and entry["index"] == 0
    assert entry["searched_at"].endswith("+00:00")
    assert entry["items"][0] == {
        "title": "TaskGroup & tasks",
        "source": "docs.python.org",
        "url": "https://docs.python.org/3/library/asyncio-task.html",
        "snippet": "A search snippet",
    }
    assert len(requests) == 1  # No page, thumbnail, redirect or summary fetch.
    wire = result.to_message(ToolCall("search", "web_search", {}))
    assert not wire.is_error
    assert "test-private-key" not in wire.content
    assert "ref_id" not in wire.content
    assert set(tool.definition.parameters["properties"]) == {"queries", "domains", "max_results"}


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        [],
        {},
        {"queries": []},
        {"queries": ["q"] * 4},
        {"queries": "q"},
        {"queries": [1]},
        {"queries": [""]},
        {"queries": [" "]},
        {"queries": ["a" * 401]},
        {"queries": ["q\nsecret"]},
        {"queries": ["q"], "domains": []},
        {"queries": ["q"], "domains": [None]},
        {"queries": ["q"], "domains": "example.com"},
        {"queries": ["q"], "domains": ["https://example.com"]},
        {"queries": ["q"], "domains": ["example.com/path"]},
        {"queries": ["q"], "domains": ["*.example.com"]},
        {"queries": ["q"], "domains": ["example.com OR evil.com"]},
        {"queries": ["q"], "domains": ["127.0.0.1"]},
        {"queries": ["q"], "domains": ["example.com"] * 6},
        {"queries": ["q"], "max_results": True},
        {"queries": ["q"], "max_results": 0},
        {"queries": ["q"], "max_results": 11},
        {"queries": ["q"], "max_results": 2.0},
        {"queries": ["q"], "max_results": "5"},
        {"queries": ["q"], "api_key": "secret"},
        {"queries": ["q"], "url": "http://127.0.0.1"},
    ],
)
def test_invalid_arguments_do_not_send_requests(make_tool, arguments):
    requests = []
    tool = make_tool(lambda req: requests.append(req))
    result = tool.execute(arguments)
    assert not result.success and result.error_code == "INVALID_ARGUMENTS"
    assert not requests


def test_domain_filter_is_strict_includes_subdomains_and_deduplicates(make_tool):
    tool = make_tool(
        lambda _: response(
            hits(
                "https://docs.python.org/a",
                "https://docs.python.org/a",
                "https://sub.docs.python.org/b",
                "https://docs.python.org.evil.com/c",
                "https://evildocs.python.org/c",
                "https://python.org/c",
                "file:///etc/passwd",
                "https://user:password@docs.python.org/x",
                "http://127.0.0.1/",
                "https://docs.python.org:999999/x",
                "https://docs.python.org\\@evil.com/x",
            )
        )
    )
    entry = tool.execute({"queries": ["q"], "domains": ["docs.python.org"]}).data["results"][0]
    assert [item["url"] for item in entry["items"]] == [
        "https://docs.python.org/a",
        "https://sub.docs.python.org/b",
    ]
    assert entry["filtered_results"] == 8


def test_batch_keeps_input_order_and_individual_errors(make_tool):
    async def handler(request):
        query = request.url.params["q"]
        if query == "slow":
            await asyncio.sleep(0.02)
            return response(hits("https://example.com/ok"))
        if query == "limit":
            return response(status=429, body=b"test-private-key must never be echoed")
        raise httpx.ConnectError("test-private-key", request=request)

    result = make_tool(handler).execute({"queries": ["slow", "limit", "fail"]})
    assert result.success
    entries = result.data["results"]
    assert [entry["query"] for entry in entries] == ["slow", "limit", "fail"]
    assert entries[0]["success"]
    assert entries[1]["error"] == {
        "code": "RATE_LIMITED",
        "message": "Search provider rate limit reached.",
        "retryable": True,
        "http_status": 429,
    }
    assert entries[2]["error"]["code"] == "NETWORK_ERROR"
    assert "test-private-key" not in json.dumps(result.data)


@pytest.mark.parametrize(
    "status,code",
    [(302, "BLOCKED_URL"), (401, "HTTP_ERROR"), (403, "HTTP_ERROR"), (500, "HTTP_ERROR")],
)
def test_http_failures_and_redirects_do_not_follow_or_echo_secrets(make_tool, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return response(
            status=status,
            headers={"location": "http://169.254.169.254/secret"},
            body=b"test-private-key",
        )

    result = make_tool(handler).execute({"queries": ["q"]})
    assert result.success  # Even an all-failed batch preserves its per-query errors.
    assert result.data["results"][0]["error"]["code"] == code
    assert len(requests) == 1 and "test-private-key" not in json.dumps(result.data)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"error": "credential"},
        {"type": "search", "web": []},
        {"type": "search", "web": {"results": [{}]}},
    ],
)
def test_malformed_response_is_not_reported_as_no_matches(make_tool, payload):
    entry = make_tool(lambda _: response(payload)).execute({"queries": ["q"]}).data["results"][0]
    assert entry["error"]["code"] == "INVALID_RESPONSE"


def test_no_web_matches_is_success(make_tool):
    result = make_tool(lambda _: response({"type": "search"})).execute({"queries": ["q"]})
    assert result.data["results"][0]["items"] == []
    assert result.data["results"][0]["success"]


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"content-type": "text/html"}, b"<html>blocked</html>", "UNSUPPORTED_CONTENT_TYPE"),
        ({"content-encoding": "br"}, b"bad", "UNSUPPORTED_CONTENT_ENCODING"),
        ({}, b"invalid", "INVALID_RESPONSE"),
        ({"content-encoding": "gzip"}, b"invalid", "INVALID_RESPONSE"),
        ({}, b" " * 1025, "RESPONSE_TOO_LARGE"),
        ({"content-encoding": "gzip"}, gzip.compress(b" " * 1025), "RESPONSE_TOO_LARGE"),
    ],
)
def test_stream_content_and_size_limits(make_tool, monkeypatch, headers, body, code):
    monkeypatch.setattr("tools._internal.web_backend.MAX_RESPONSE_BYTES", 1024)
    result = make_tool(lambda _: response(headers=headers, body=body)).execute({"queries": ["q"]})
    assert result.data["results"][0]["error"]["code"] == code


def test_gzip_search_response(make_tool):
    body = gzip.compress(json.dumps(hits("https://example.com/")).encode())
    result = make_tool(lambda _: response(headers={"content-encoding": "gzip"}, body=body)).execute(
        {"queries": ["q"]}
    )
    assert result.data["results"][0]["success"]


def test_total_deadline_stops_continuously_arriving_data_and_closes_stream(make_tool):
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    tool = make_tool(
        lambda _: httpx.Response(200, headers={"content-type": "application/json"}, stream=stream),
        timeout=0.05,
    )
    started = time.monotonic()
    result = tool.execute({"queries": ["q"]})
    assert result.data["results"][0]["error"]["code"] == "TIMEOUT"
    assert stream.closed and time.monotonic() - started < 1


def test_concurrency_limit_is_shared_across_batches(make_tool):
    lock = threading.Lock()
    active = peak = 0

    async def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
            return response()
        finally:
            with lock:
                active -= 1

    tool = make_tool(handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(tool.execute, {"queries": ["a", "b", "c"]}) for _ in range(2)]
        assert all(f.result().success for f in futures)
    assert peak == 3 and active == 0


def test_dns_work_cannot_delay_deadline_or_start_late_request():
    finished = threading.Event()
    connected = []

    def resolve():
        try:
            time.sleep(0.2)
        finally:
            finished.set()

    class SlowDNSAdapter:
        name = "test"

        async def search(self, *args):
            await asyncio.get_running_loop().run_in_executor(None, resolve)
            connected.append(True)
            return {"items": []}

    backend = WebBackend(SlowDNSAdapter(), timeout_seconds=0.02)
    started = time.monotonic()
    try:
        result = WebSearchTool(backend).execute({"queries": ["q"]})
        assert result.data["results"][0]["error"]["code"] == "TIMEOUT"
    finally:
        backend.close()
    assert time.monotonic() - started < 0.15
    assert finished.wait(1)
    assert not connected
    assert not backend._thread.is_alive()


def test_output_budget_preserves_each_query_and_whole_urls(make_tool):
    urls = ["https://example.com/" + "a" * 1800 + str(i) for i in range(10)]
    tool = make_tool(lambda _: response(hits(*urls, snippet="文" * 5000, title="T" * 1000)))
    result = tool.execute({"queries": ["a", "b", "c"], "max_results": 10})
    assert (
        len(json.dumps({"success": True, "data": result.data}, ensure_ascii=False))
        <= MAX_OUTPUT_CHARS
    )
    for entry in result.data["results"]:
        assert entry["items"] and entry["truncated"]
        assert all(item["url"] in urls for item in entry["items"])


def test_provider_query_limit_keeps_sibling_success(make_tool):
    queries = ["word " * 74, "short"]
    tool = make_tool(lambda _: response())
    result = tool.execute({"queries": queries, "domains": ["docs.python.org", "example.com"]})
    assert result.data["results"][0]["error"]["code"] == "INVALID_ARGUMENTS"
    assert result.data["results"][1]["success"]


def test_search_is_opt_in_and_absent_from_worker_factory(tmp_path):
    assert WebBackend.from_environment({}) is None
    assert (
        WebBackend.from_environment(
            {"AGENT_WEB_SEARCH_PROVIDER": "off", "BRAVE_SEARCH_API_KEY": "secret"}
        )
        is None
    )
    assert create_web_tools(None) == []
    for isolated in (False, True):
        assert "web_search" not in {
            t.definition.name
            for t in create_default_tools(
                tmp_path,
                isolated_execution=isolated,
            )
        }
    with pytest.raises(ValueError, match="must be off or brave"):
        WebBackend.from_environment({"AGENT_WEB_SEARCH_PROVIDER": "other"})
    with pytest.raises(ValueError, match="Set BRAVE_SEARCH_API_KEY"):
        WebBackend.from_environment({"AGENT_WEB_SEARCH_PROVIDER": "brave"})
    backend = WebBackend.from_environment(
        {"AGENT_WEB_SEARCH_PROVIDER": "brave", "BRAVE_SEARCH_API_KEY": "secret"}
    )
    try:
        assert [tool.definition.name for tool in create_web_tools(backend)] == ["web_search"]
    finally:
        backend.close()
