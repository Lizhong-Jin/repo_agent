"""Cache ratios describe the last request's input, never cumulative/output tokens."""

import pytest

from agent import AgentRuntime
from agent.Tracing import ModelCallRecord, RunStats
from cli.session_status import SessionStatus
from llm import Usage


@pytest.mark.parametrize(
    "incoming,cached,expected",
    [
        (1000, 800, "80.0%"),
        (1000, 0, "0.0%"),
        (1000, 1000, "100.0%"),
        (1000, None, "未知"),
        (None, 800, "未知"),
        (0, 0, "未知"),
        (100, 200, "未知"),
    ],
)
def test_cache_ratio_uses_only_valid_input_counters(tmp_path, incoming, cached, expected):
    status = SessionStatus(tmp_path, context_window=10000)
    stats = RunStats(1)
    stats.model_calls.append(
        ModelCallRecord(
            1,
            usage=Usage(incoming, 5000, cached_input_tokens=cached, cache_write_tokens=100),
        )
    )
    status("model_end", stats)
    assert status.describe_cache() == f"缓存命中 {expected}"
    assert f"缓存命中 {expected}" in status.describe(compact=True)


def test_latest_request_replaces_cache_ratio_even_when_usage_is_missing(tmp_path):
    status = SessionStatus(tmp_path)
    stats = RunStats(1)
    for step, (usage, expected) in enumerate(
        [
            (Usage(1000, 50, cached_input_tokens=900), "90.0%"),
            (Usage(200, None, cached_input_tokens=50), "25.0%"),
            (Usage(100, 5), "未知"),
        ],
        1,
    ):
        stats.model_calls.append(ModelCallRecord(step, usage=usage))
        status("model_end", stats)
        assert status.describe_cache() == f"缓存命中 {expected}"
    assert status.totals["input_tokens"] == 1300


def test_cache_restore_legacy_and_resets(tmp_path):
    status = SessionStatus(tmp_path)
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(100, 10, cached_input_tokens=80)))
    status("model_end", stats)
    saved = status.session_state()
    restored = SessionStatus(tmp_path)
    restored.restore_session(saved)
    assert restored.describe_cache() == "缓存命中 80.0%"
    restored.reset_context()
    assert restored.describe_cache() == "缓存命中 未知"
    assert restored.totals["input_tokens"] == 100
    restored.restore_session(saved, context=False)
    assert restored.describe_cache() == "缓存命中 未知"
    restored.restore_session(saved)
    restored.reset_session()
    assert restored.describe_cache() == "缓存命中 未知"
    assert restored.calls == 0
    saved.pop("context_cached_input_tokens")
    restored.restore_session(saved)
    assert restored.describe_cache() == "缓存命中 未知"


def test_local_estimate_never_becomes_cache_denominator(tmp_path):
    status = SessionStatus(tmp_path)
    stats = RunStats(1)
    stats.model_calls.append(ModelCallRecord(1, usage=Usage(100, None, cached_input_tokens=80)))
    status("model_end", stats)
    assert status.describe_cache() == "缓存命中 80.0%"
    status.initialize_context(AgentRuntime(object()))
    assert "本地粗估" in status.describe_context()
    assert status.describe_cache() == "缓存命中 未知"
