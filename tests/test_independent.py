from dataclasses import replace

import pytest

from llm import LLMConfig
from llm.independent import IndependentRequestPolicy
from llm.model_catalog import MODEL_CATALOG, model_info


@pytest.mark.parametrize(
    "info",
    [info for group in MODEL_CATALOG.values() for info in group.values()],
    ids=lambda info: f"{info.provider}/{info.id}",
)
def test_every_catalog_model_has_valid_independent_policy(info):
    policy = IndependentRequestPolicy.from_config(LLMConfig(info.provider, info.id, api_key="fake"))
    assert policy.thinking and policy.thinking == info.independent_thinking
    assert 0 < policy.initial_output_tokens <= policy.max_output_tokens
    policy.extras(policy.initial_output_tokens)
    policy.extras(policy.max_output_tokens)


@pytest.mark.parametrize(
    "maximum,initial",
    [(4096, 4096), (32000, 8192), (64000, 16000), (128000, 32000), (393216, 32768), (None, 8192)],
)
def test_output_proportion_and_bounds(monkeypatch, maximum, initial):
    info = model_info("deepseek", "deepseek-flash")
    monkeypatch.setitem(
        MODEL_CATALOG["deepseek"], info.id, replace(info, max_output_tokens=maximum)
    )
    policy = IndependentRequestPolicy.from_config(LLMConfig(info.provider, info.id, api_key="fake"))
    assert policy.initial_output_tokens == initial
    assert policy.max_output_tokens == (maximum or 8192)
    assert policy.output_limit_known is (maximum is not None)


def test_catalog_configuration_is_copied_and_snapshots_inherit_policy():
    info = model_info("openai", "gpt-5")
    snapshot = model_info("openai", "gpt-5-2026-01-01", allow_snapshot=True)
    assert info.independent_thinking == snapshot.independent_thinking
    info.independent_thinking["effort"] = "high"
    assert model_info("openai", "gpt-5").independent_thinking["effort"] == "minimal"
    policy = IndependentRequestPolicy.from_config(LLMConfig("openai", snapshot.id, api_key="fake"))
    assert policy.initial_output_tokens == 8192 and not policy.output_limit_known
