import argparse
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from agent import AgentRuntime
from cli.live import ThinkingControl
from cli.models import ModelControl, ModelSelection
from cli.settings import add_runtime_arguments, request_options
from cli.thinking_store import (
    load_preference,
    preference_path,
    restore_thinking_args,
    save_preference,
)
from llm import ConfigurationError, LLMClient, LLMConfig, LLMRequest, Message
from llm.thinking import thinking_options
from llm.thinking_profiles import history_default, thinking_profile


def arguments(provider="zhipu", model="glm-5.3", flags=(), base_url=None):
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(list(flags))
    args.provider, args.model, args.base_url = provider, model, base_url
    return args


def control(provider="zhipu", model="glm-5.3", flags=(), base_url=None):
    args = arguments(provider, model, flags, base_url)
    restore_thinking_args(args)
    runtime = AgentRuntime(object(), request_extra=request_options(args))
    return ThinkingControl(runtime, args)


@pytest.mark.parametrize(
    "provider,model,levels",
    [
        ("zhipu", "glm-5.3", ["low", "high", "max"]),
        ("zhipu", "glm-5.2", ["high", "max"]),
        ("zhipu", "glm-4.7", []),
        ("openai", "gpt-5-2025-08-07", ["minimal", "low", "medium", "high"]),
        ("openai", "gpt-5.2", ["low", "medium", "high", "xhigh"]),
        ("anthropic", "claude-sonnet-4-6", ["low", "medium", "high"]),
        ("anthropic", "claude-opus-4-6", ["low", "medium", "high", "max"]),
        ("gemini", "gemini-3-pro-preview", ["low", "high"]),
        ("gemini", "gemini-3-flash-preview", ["minimal", "low", "medium", "high"]),
    ],
)
def test_model_specific_presets(provider, model, levels):
    c = control(provider, model)
    assert [effort for _, effort, _ in c.presets() if effort is not None] == levels
    for preset in c.presets():
        c.set(*preset)
        assert c.runtime.request_extra == thinking_options(
            provider,
            model,
            **c.current,
            max_output_tokens=c.limit,
        )


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-5"), ("zhipu", "glm-5.3"), ("gemini", "gemini-2.5-pro")]
)
def test_forced_thinking_rejects_off_without_changing_settings(provider, model):
    c = control(provider, model)
    before = deepcopy((c.current, c.runtime.request_extra))
    with pytest.raises(ConfigurationError, match="不能关闭"):
        c.command("/thinking off")
    assert (c.current, c.runtime.request_extra) == before
    assert not preference_path().exists()


def test_adaptive_shortcut_and_fixed_budget_constraints():
    c = control("anthropic", "claude-sonnet-4-6")
    c.command("/thinking low")
    assert c.current["mode"] == "adaptive"
    assert c.runtime.request_extra == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }
    c.command("/thinking on high")
    assert c.current["mode"] == "adaptive"
    with pytest.raises(ConfigurationError):
        c.command("/thinking budget 2048")
    c = control("anthropic", "claude-sonnet-4-5")
    assert all(b < c.limit for _, _, b in c.presets() if b)
    c.command("/thinking budget 2048")
    assert c.runtime.request_extra == {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    with pytest.raises(ConfigurationError):
        c.command("/thinking budget 4096")


def test_history_parameter_preserves_full_native_reasoning_and_tools():
    c = control()
    c.command("/thinking high")
    c.command("/thinking history on")
    assert c.runtime.request_extra == {
        "thinking": {"type": "enabled", "clear_thinking": False},
        "reasoning_effort": "high",
    }
    # Replay an actual decoded assistant tool call, including unmodified reasoning.
    from llm.adapters import ADAPTERS

    adapter = ADAPTERS["chat_completions"]("zhipu", "glm-5.3")
    native = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "opaque reasoning",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"x"}'},
            }
        ],
    }
    response = adapter.decode({"choices": [{"message": native, "finish_reason": "tool_calls"}]})
    payload = adapter.encode(
        LLMRequest(
            [
                Message("user", "task"),
                response.message,
                Message("tool", "result", tool_call_id="call-1", name="read"),
            ],
            extra=c.runtime.request_extra,
        )
    )
    assert payload["messages"][1] == native
    c.cycle()
    assert c.current["history"] == "on"
    c.command("/thinking history off")
    assert c.runtime.request_extra["thinking"]["clear_thinking"] is True
    c.command("/thinking history auto")
    assert "clear_thinking" not in c.runtime.request_extra.get("thinking", {})


def test_endpoint_defaults_and_preferences_are_isolated():
    c = control(base_url="https://open.bigmodel.cn/api/paas/v4/")
    c.command("/thinking low")
    c.command("/thinking history on")
    restored = control()
    assert restored.current == c.current
    other = control(base_url="https://open.bigmodel.cn/api/coding/paas/v4")
    assert other.current["mode"] == "auto"
    assert "默认：保留" in other.describe()
    assert "默认：清除跨轮思考" in control(flags=["--thinking", "auto"]).describe()
    assert history_default("zhipu", "https://gateway.invalid/v1") == "由接口决定"
    assert preference_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "flags", [["--thinking", "auto"], ["--reasoning-effort", "max"], ["--thinking-recall", "false"]]
)
def test_explicit_flags_win_over_remembered_settings(flags):
    control().command("/thinking low")
    c = control(flags=flags)
    assert c.current["effort"] != "low"


def test_nondefault_environment_wins_and_reset_forgets(monkeypatch):
    c = control()
    c.command("/thinking high")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    assert control().current["effort"] == "low"
    monkeypatch.delenv("LLM_REASONING_EFFORT")
    assert control().current["effort"] == "high"
    c.command("/thinking reset")
    assert load_preference("zhipu", "glm-5.3") is None
    assert control().current == {"mode": "auto", "effort": None, "budget": None, "history": "auto"}


def test_unknown_model_has_no_speculative_shortcut_but_explicit_parameters_work():
    c = control(model="private-model")
    assert c.presets() == [("auto", None, None)]
    assert "能力未确认" in c.command("/thinking list")
    c.command("/thinking on high")
    assert c.runtime.request_extra["reasoning_effort"] == "high"


def test_custom_model_profile_is_validated_and_saved_with_preference():
    profile = {
        "modes": ["auto", "enabled"],
        "efforts": ["low", "high"],
        "aliases": {"medium": "high"},
    }
    c = control(model="private-model", flags=["--thinking-profile", json.dumps(profile)])
    c.command("/thinking medium")
    assert c.current["effort"] == "high"
    restored = control(model="private-model")
    assert restored.override == profile
    assert restored.runtime.request_extra == c.runtime.request_extra
    assert restored.presets() == [
        ("auto", None, None),
        ("enabled", "low", None),
        ("enabled", "high", None),
    ]


@pytest.mark.parametrize(
    "profile",
    [
        [],
        {"modes": ["disabled"]},
        {"modes": ["auto", "magic"]},
        {"history": "yes"},
        {"budget_min": True},
        {"efforts": ["invented"]},
        {"unknown": 1},
        {"modes": ["auto"], "efforts": ["high"]},
        {"budget_min": 10, "budget_max": 5},
    ],
)
def test_invalid_custom_profile_fails_locally(profile):
    with pytest.raises(ConfigurationError):
        thinking_profile("zhipu", "private-model", override=profile)


def test_failed_persistence_does_not_change_live_settings(monkeypatch):
    c = control()
    c.command("/thinking low")
    before = deepcopy((c.current, c.runtime.request_extra, c.runtime.thinking_settings))
    raw = preference_path().read_bytes()

    def fail(*a):
        raise OSError("disk full")

    monkeypatch.setattr("cli.thinking_store.os.replace", fail)
    with pytest.raises(OSError):
        c.command("/thinking max")
    assert (c.current, c.runtime.request_extra, c.runtime.thinking_settings) == before
    assert preference_path().read_bytes() == raw
    assert not list(preference_path().parent.glob(".thinking-*"))


def test_corrupt_preference_is_not_overwritten_and_recall_can_be_disabled():
    c = control()
    c.command("/thinking low")
    preference_path().write_text("not JSON")
    with pytest.raises(ConfigurationError):
        c.command("/thinking high")
    assert preference_path().read_text() == "not JSON"
    assert c.current["effort"] == "low"
    other = control(flags=["--thinking-recall", "false"])
    other.command("/thinking max")
    assert preference_path().read_text() == "not JSON"


def test_model_switch_restores_target_and_bad_preference_rolls_back(tmp_path):
    control("zhipu", "glm-5.3").command("/thinking high")
    initial = LLMConfig("openai", "gpt-5", api_key="private-key")
    clients = []

    def factory(config):
        client = LLMClient(
            config,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: pytest.fail("Switching thinking must not probe model API")
                )
            ),
        )
        clients.append(client)
        return client

    c = control("openai", "gpt-5")
    c.runtime.llm = factory(initial)
    models = ModelControl(c.runtime, initial, thinking=c, client_factory=factory)
    try:
        models.switch(ModelSelection("zhipu", "glm-5.3", "second-key"))
        assert c.current["effort"] == "high"
        assert c.runtime.request_extra["reasoning_effort"] == "high"
        c.command("/thinking low")
        models.switch(ModelSelection("openai", "gpt-5", "private-key"))
        assert "minimal" in c.command("/thinking list")
        models.switch(ModelSelection("zhipu", "glm-5.3", "second-key"))
        assert c.current["effort"] == "low"
        before = c.runtime.llm
        save_preference(
            "anthropic",
            "claude-sonnet-4-5",
            None,
            {"mode": "enabled", "effort": None, "budget": 8000, "history": "auto"},
            {},
        )
        with pytest.raises(ConfigurationError):
            models.switch(ModelSelection("anthropic", "claude-sonnet-4-5", "third-key"))
        assert c.runtime.llm is before and c.model == "glm-5.3"
        assert c.current["effort"] == "low"
        assert "private-key" not in preference_path().read_text()
        assert "second-key" not in preference_path().read_text()
    finally:
        for client in clients:
            client._http.close()


def test_non_thinking_extra_does_not_block_preference_restore():
    control().command("/thinking low")
    c = control(flags=["--extra-json", '{"top_p":0.9}'])
    assert c.runtime.request_extra["top_p"] == 0.9
    assert c.runtime.request_extra["reasoning_effort"] == "low"


@pytest.mark.parametrize("value", ["[]", "null", "invalid"])
def test_bad_extra_reports_configuration_error_before_restoring(value):
    with pytest.raises(ConfigurationError, match="LLM_EXTRA_JSON"):
        control(flags=["--extra-json", value])


def test_config_validation_uses_same_history_and_profile_rules():
    from cli.config_command import validate_values

    values = {"LLM_PROVIDER": "zhipu", "LLM_MODEL": "glm-5.3", "LLM_THINKING_HISTORY": "on"}
    validate_values(values)
    with pytest.raises(ValueError, match="配置组合冲突"):
        validate_values({**values, "LLM_PROVIDER": "gemini", "LLM_MODEL": "gemini-3-pro-preview"})
    profile = {"modes": ["auto", "enabled"], "efforts": ["low", "high"]}
    validate_values(
        {
            "LLM_PROVIDER": "zhipu",
            "LLM_MODEL": "custom",
            "LLM_THINKING": "enabled",
            "LLM_REASONING_EFFORT": "low",
            "LLM_THINKING_PROFILE": json.dumps(profile),
        }
    )


def test_model_client_validation_failure_does_not_save_or_change(monkeypatch):
    c = control()
    c.command("/thinking low")
    before = deepcopy((c.current, c.runtime.request_extra))
    raw = preference_path().read_bytes()

    class Adapter:
        def encode(self, request):
            raise ConfigurationError("cannot encode")

    c.runtime.llm = SimpleNamespace(adapter=Adapter())
    with pytest.raises(ConfigurationError):
        c.command("/thinking high")
    assert (c.current, c.runtime.request_extra) == before
    assert preference_path().read_bytes() == raw


def test_budget_preferences_revalidate_against_new_output_limit():
    c = control("anthropic", "claude-sonnet-4-5", flags=["--max-output-tokens", "16000"])
    c.command("/thinking budget 8192")
    with pytest.raises(ConfigurationError):
        control("anthropic", "claude-sonnet-4-5", flags=["--max-output-tokens", "4096"])
    c = control("anthropic", "claude-sonnet-4-5", flags=["--thinking", "auto"])
    c.command("/thinking reset")
    assert control("anthropic", "claude-sonnet-4-5").current["budget"] is None


def test_explicit_on_displays_actual_effort_sent():
    c = control("openai", "gpt-5.2")
    c.command("/thinking on")
    assert c.current["effort"] == c.runtime.request_extra["reasoning"]["effort"]
    c = control()
    c.command("/thinking on")
    assert c.current["effort"] == c.runtime.request_extra["reasoning_effort"] == "max"


def test_catalog_extension_and_explicit_snapshot_precedence(monkeypatch):
    from llm.model_catalog import MODEL_CATALOG, ModelInfo

    monkeypatch.setitem(
        MODEL_CATALOG, "openai", {
            "private-reasoner": ModelInfo(
                "openai", "private-reasoner",
                thinking={"modes": ("auto", "enabled"), "efforts": ("low", "high")},
                dated_snapshots=True,
            ),
            "private-reasoner-2026-09-19": ModelInfo(
                "openai", "private-reasoner-2026-09-19",
                thinking={"modes": ("auto", "enabled"), "efforts": ("high",)},
            ),
            "exact-only": ModelInfo(
                "openai", "exact-only", thinking={"modes": ("auto", "disabled", "enabled")},
            ),
        },
    )
    assert thinking_profile("openai", "PRIVATE-REASONER").efforts == ("low", "high")
    assert thinking_profile("openai", "private-reasoner-20260918").efforts == ("low", "high")
    assert thinking_profile("openai", "private-reasoner-2026-09-19").efforts == ("high",)
    assert not thinking_profile("openai", "private-reasoner-next").known
    assert not thinking_profile("openai", "exact-only-20260918").known
    assert not thinking_profile("gemini", "private-reasoner").known


def test_returned_profile_cannot_mutate_catalog_aliases():
    profile = thinking_profile("zhipu", "glm-5.3")
    profile.aliases["medium"] = "low"
    assert thinking_profile("zhipu", "glm-5.3").aliases["medium"] == "high"
    assert thinking_profile("zhipu", "glm-5.3-flash").aliases["medium"] == "high"
