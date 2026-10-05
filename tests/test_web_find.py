"""Cached snapshot search: no network, Unicode locations, paging and cancellation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_web_fetch import fetch, reply
from test_web_fetch import make_fetch as make_fetch

from host_support.cancellation import RunCancelled, cancellation_scope
from llm import ToolCall
from tools import ExecutionKind, ToolEffects
from tools._internal.web_backend import WebBackend
from tools._internal.web_pages import PageCache
from tools.factory import create_default_tools
from tools.scheduling import INDEPENDENT
from tools.web_tools import WebFindTool, create_web_tools


def find(tool, ref_id, query, **options):
    return WebFindTool(tool.backend).execute({"ref_id": ref_id, "query": query, **options})


def test_literal_find_returns_first_match_per_line_and_fetch_compatible_context(make_fetch):
    tool, network = make_fetch(lambda _: reply("before\nNeedle NEEDLE\na.b\nneedle\nafter"))
    page = fetch(tool, url="https://docs.example.org")
    args = {"ref_id": page["ref_id"], "query": "needle", "context_lines": 1}
    original = deepcopy(args)
    result = WebFindTool(tool.backend).execute(args)
    assert result.success and result.effects == ToolEffects("none") and args == original
    assert result.data["untrusted"] and result.data["returned_matches"] == 2
    assert result.data["next_start_line"] is None and not result.data["truncated"]
    assert [m["line"] for m in result.data["matches"]] == [2, 4]
    for match in result.data["matches"]:
        cached = fetch(
            tool,
            ref_id=page["ref_id"],
            start_line=match["start_line"],
            line_count=match["end_line"] - match["start_line"] + 1,
        )
        assert match["content"] == cached["content"]
    assert result.data["matches"][0]["column"] == 1
    assert result.data["matches"][0]["end_column"] == 7
    assert find(tool, page["ref_id"], "needle", case_sensitive=True).data["returned_matches"] == 1
    assert find(tool, page["ref_id"], "a.b").data["matches"][0]["line"] == 3
    assert find(tool, page["ref_id"], ".*").data["matches"] == []
    assert len(network.streams) == len(network.resolved) == 1


@pytest.mark.parametrize(
    "text,query,column,end_column",
    [
        ("🙂Straße", "STRASSE", 2, 8),
        ("前ﬃ后", "FFI", 2, 3),
        ("İx", "i\u0307", 1, 2),
        ("😀你好", "你好", 2, 4),
        ("ßx", "s", 1, 2),
    ],
)
def test_casefold_spans_use_original_unicode_columns(make_fetch, text, query, column, end_column):
    tool, _ = make_fetch(lambda _: reply(text))
    page = fetch(tool, url="https://docs.example.org")
    match = find(tool, page["ref_id"], query).data["matches"][0]
    assert (match["column"], match["end_column"]) == (column, end_column)
    assert match["content"] == text


def test_result_limit_pagination_no_duplicates_and_exact_end(make_fetch):
    tool, _ = make_fetch(lambda _: reply("yes\nno\nyes\nno\nyes\nno"))
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    first = find(tool, ref, "yes", max_results=2).data
    assert first["truncated"] and first["truncation_reason"] == "max_results"
    assert first["next_start_line"] == 5
    second = find(tool, ref, "yes", max_results=1, start_line=first["next_start_line"]).data
    assert [m["line"] for m in first["matches"] + second["matches"]] == [1, 3, 5]
    assert not second["truncated"] and second["next_start_line"] is None
    assert find(tool, ref, "yes", start_line=7).error_code == "INVALID_RANGE"


def test_output_budget_preserves_whole_matches_and_resume(make_fetch, monkeypatch):
    tool, _ = make_fetch(lambda _: reply("\n".join("needle " + "x" * 400 for _ in range(20))))
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    monkeypatch.setattr("tools._internal.web_backend.MAX_OUTPUT_CHARS", 2500)
    seen = []
    start = 1
    while True:
        result = find(tool, ref, "needle", context_lines=0, start_line=start)
        assert result.success
        assert len(result.to_message(ToolCall("find", "web_find", {})).content) <= 2500
        page = result.data
        assert page["matches"]
        seen += [m["line"] for m in page["matches"]]
        assert all(m["content"] == "needle " + "x" * 400 for m in page["matches"])
        if page["next_start_line"] is None:
            break
        assert page["truncation_reason"] == "output_budget"
        start = page["next_start_line"]
    assert seen == list(range(1, 21))
    assert find(tool, ref, "needle", context_lines=5).error_code == "OUTPUT_TOO_LARGE"


def test_empty_and_no_cross_line_matching(make_fetch):
    tool, _ = make_fetch(lambda _: reply(""))
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    result = find(tool, ref, "absent")
    assert result.success and result.data["total_lines"] == 0
    assert not result.data["matches"] and result.data["next_start_line"] is None
    assert find(tool, ref, "a\nb").error_code == "INVALID_ARGUMENTS"


def test_expired_foreign_and_evicted_refs_never_refetch(make_fetch):
    now = [0]
    tool, network = make_fetch(cache=PageCache(ttl=10, max_entries=1, clock=lambda: now[0]))
    a = fetch(tool, url="https://docs.example.org")["ref_id"]
    b = fetch(tool, url="https://docs.example.org/next")["ref_id"]
    assert find(tool, a, "hello").error_code == "REF_EXPIRED"
    now[0] = 11
    assert find(tool, b, "hello").error_code == "REF_EXPIRED"
    other, other_network = make_fetch()
    assert find(other, b, "hello").error_code == "REF_NOT_FOUND"
    assert find(other, "doc_" + "0" * 32, "hello").error_code == "REF_NOT_FOUND"
    assert len(network.streams) == 2 and not other_network.streams


@pytest.mark.parametrize(
    "patch",
    [
        {"ref_id": "https://example.org"},
        {"ref_id": None},
        {"query": ""},
        {"query": " "},
        {"query": "x" * 401},
        {"query": "\x00"},
        {"query": []},
        {"case_sensitive": 1},
        {"start_line": True},
        {"start_line": 0},
        {"start_line": 10000001},
        {"max_results": 51},
        {"max_results": 0},
        {"max_results": 1.5},
        {"context_lines": -1},
        {"context_lines": 6},
        {"context_lines": True},
        {"url": "https://example.org"},
        {"regex": True},
    ],
)
def test_invalid_arguments_do_not_touch_backend(patch):
    class Backend:
        def find(self, arguments):
            pytest.fail("Invalid arguments reached backend")

    result = WebFindTool(Backend()).execute({"ref_id": "doc_" + "a" * 32, "query": "q", **patch})
    assert result.error_code == "INVALID_ARGUMENTS"


def test_concurrent_finds_share_immutable_snapshot_without_network(make_fetch):
    tool, network = make_fetch(lambda _: reply("Alpha\nBeta\nALPHA"))
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: find(tool, ref, "alpha"), range(20)))
    assert all(r.data == results[0].data for r in results)
    assert len(network.streams) == 1


def test_timeout_cancellation_and_closed_backend_are_safe(make_fetch, monkeypatch):
    tool, _ = make_fetch(timeout=0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(tool.backend.pages, "find", slow)
    assert find(tool, "doc_" + "0" * 32, "x").error_code == "TIMEOUT"
    with cancellation_scope() as context:
        context.cancel()
        with pytest.raises(RunCancelled):
            find(tool, "doc_" + "0" * 32, "x")
    tool.backend.close()
    assert find(tool, "doc_" + "0" * 32, "x").error_code == "FIND_UNAVAILABLE"


def test_registration_is_cache_only_host_control_and_not_in_workers(tmp_path):
    backend = WebBackend(fetch_enabled=True)
    try:
        tools = create_web_tools(backend)
        finder = next(t for t in tools if t.definition.name == "web_find")
        assert finder.execution_kind is ExecutionKind.HOST_CONTROL
        assert finder.scheduling_policy is INDEPENDENT
    finally:
        backend.close()
    backend = WebBackend()
    try:
        assert not create_web_tools(backend)
    finally:
        backend.close()
    for isolated in (False, True):
        assert "web_find" not in {
            t.definition.name for t in create_default_tools(tmp_path, isolated_execution=isolated)
        }


def test_search_cancels_during_scan_and_backend_remains_usable(make_fetch, monkeypatch):
    from tools._internal import web_pages

    tool, _ = make_fetch(lambda _: reply("\n".join("nothing" for _ in range(500))))
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    original = web_pages.literal_span
    calls = 0
    with cancellation_scope() as context:

        def match(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 10:
                context.cancel()
            return original(*args, **kwargs)

        monkeypatch.setattr(web_pages, "literal_span", match)
        with pytest.raises(RunCancelled):
            find(tool, ref, "absent")
    assert 10 <= calls < 500
    monkeypatch.setattr(web_pages, "literal_span", original)
    assert find(tool, ref, "nothing", max_results=1).success


def test_new_fetch_does_not_change_old_search_and_wrapped_line_numbers_match(make_fetch):
    tool, network = make_fetch(lambda _: reply("x" * 512 + "needle"))
    old_ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    network.respond = lambda _: reply("new page")
    new_ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    result = find(tool, old_ref, "needle", context_lines=0)
    assert result.data["long_lines_wrapped"]
    assert result.data["matches"][0]["line"] == 2
    assert result.data["matches"][0]["column"] == 1
    assert find(tool, new_ref, "needle").data["matches"] == []
    assert len(network.streams) == 2


def test_actual_scan_checks_deadline(make_fetch):
    from tools._internal.web_pages import find_in_snapshot

    tool, _ = make_fetch()
    ref = fetch(tool, url="https://docs.example.org")["ref_id"]
    snapshot = tool.backend.pages.cache.get(ref)
    options = WebFindTool._arguments({"ref_id": ref, "query": "hello"})
    with pytest.raises(TimeoutError):
        asyncio.run(find_in_snapshot(snapshot, options, budget=23000, deadline=0))
