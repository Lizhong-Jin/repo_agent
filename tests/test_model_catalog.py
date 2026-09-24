from types import SimpleNamespace

import httpx
import pytest
from prompt_toolkit.mouse_events import MouseEventType

from cli.live import SessionStatus
from cli.model_picker import ModelPicker, pick_model
from llm import LLMClient, LLMConfig
from llm.model_catalog import (
    MODEL_CATALOG,
    model_info,
    require_supported_model,
    supported_models,
    supported_providers,
)
from llm.thinking_profiles import thinking_profile


def test_catalog_profiles_and_limits_are_consistent():
    assert len(supported_providers()) == 8
    for provider, models in MODEL_CATALOG.items():
        for key, info in models.items():
            assert key == info.id.lower()
            assert info.provider == provider
            assert info.sources and info.checked_on
            assert info.context_window is None or info.context_window > 0
            assert info.max_output_tokens is None or info.max_output_tokens > 0
            assert info.min_thinking and info.max_thinking
            profile = thinking_profile(provider, info.id)
            assert profile.known
            if profile.default_effort:
                assert profile.default_effort in profile.efforts
            assert set(profile.aliases.values()) <= set(profile.efforts)
            if info.selectable:
                assert require_supported_model(provider, info.id) == info


def test_thinking_bounds_preserve_off_mandatory_reasoning_and_unknown_budget():
    assert model_info("openai", "gpt-5").min_thinking == "minimal"
    assert model_info("openai", "gpt-5.2").min_thinking == "disabled"
    assert model_info("openai", "gpt-5.2").max_thinking == "xhigh"
    assert model_info("gemini", "gemini-2.5-pro").min_thinking == "budget:128"
    assert model_info("gemini", "gemini-2.5-pro").max_thinking == "budget:32768"
    assert model_info("anthropic", "claude-sonnet-4-5").max_thinking == "budget:unknown"
    assert model_info("minimax", "MiniMax-M2.5").min_thinking == "enabled"
    assert model_info("minimax", "MiniMax-M2.5").max_output_tokens is None
    assert model_info("zhipu", "glm-5.3-flash").context_window == 1000000


def test_exact_lookup_aliases_case_and_snapshot_limits():
    assert model_info("chatgpt", "GPT-5").id == "gpt-5"
    assert require_supported_model("minimax", "minimax-m2.5").id == "MiniMax-M2.5"
    assert model_info("openai", "gpt-5-2026-01-01") is None
    snapshot = model_info("openai", "gpt-5-2026-01-01", allow_snapshot=True)
    assert snapshot.context_window is None and snapshot.max_output_tokens is None
    assert thinking_profile("openai", snapshot.id).known
    for name in ("gpt-5-2026-01-01", "invented-model"):
        with pytest.raises(ValueError):
            require_supported_model("openai", name)
    with pytest.raises(ValueError):
        require_supported_model("gemini", "gemini-3-pro-preview")
    assert "gemini-3-pro-preview" not in {m.id for m in supported_models("gemini")}


def test_catalog_readers_do_not_expose_mutable_thinking():
    model_info("zhipu", "glm-5.3").thinking["aliases"]["medium"] = "low"
    supported_models("zhipu")[0].thinking["modes"] = ()
    assert thinking_profile("zhipu", "glm-5.3").aliases["medium"] == "high"
    assert thinking_profile("zhipu", "glm-5.3").modes


@pytest.mark.parametrize(
    "provider,model,kind,tokens",
    [
        ("openai", "gpt-5.2", "context", 400000),
        ("gemini", "gemini-2.5-pro", "input", 1048576),
    ],
)
def test_context_falls_back_to_catalog_and_server_refresh_wins(
    tmp_path, provider, model, kind, tokens
):
    payloads = iter(
        [
            {},
            (
                {"name": "models/" + model, "inputTokenLimit": 12345}
                if provider == "gemini"
                else {"data": [{"id": model, "context_length": 12345}]}
            ),
        ]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(payloads)))
    ) as http:
        client = LLMClient(LLMConfig(provider, model, api_key="key"), http_client=http)
        limit = client.get_context_limit()
        assert (limit.tokens, limit.kind) == (tokens, kind)
        assert "目录" in limit.source
        status = SessionStatus(tmp_path)
        status.bind_context_model(client)
        assert "目录" in status.describe_context()
        assert status.context_window == tokens
        status.context_command("/context 1000")
        assert status.context_window == 1000
        status.context_command("/context auto")
        assert status.context_window == 12345
        assert status.context_limit_source == "服务端自动获取"


def test_picker_filters_case_insensitively_scrolls_and_never_accepts_free_text():
    picker = ModelPicker("qwen", "qwen-plus")
    picker.search("QWEN 3")
    assert picker.matches and all("3" in m.id for m in picker.matches)
    picker.move(100)
    assert picker.selected == picker.matches[-1]
    fragments = picker.fragments()
    assert picker.selected.id in "".join(p[1] for p in fragments)
    assert "最大输出" in "".join(p[1] for p in fragments)
    picker.mouse_handler(0)(SimpleNamespace(event_type=MouseEventType.SCROLL_UP))
    assert picker.index == len(picker.matches) - 2
    picker.search("missing-model")
    picker.move(-1)
    assert not picker.matches
    with pytest.raises(ValueError, match="没有匹配"):
        picker.value()
    picker.search("qwen-plus")
    assert picker.value() == "qwen-plus"


def test_startup_picker_real_search_navigation_and_cancel():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text("gpt-5\x1b[B\r")
        assert (
            pick_model("openai", terminal_input=pipe, terminal_output=DummyOutput()) == "gpt-5-mini"
        )
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            pick_model("openai", terminal_input=pipe, terminal_output=DummyOutput())
