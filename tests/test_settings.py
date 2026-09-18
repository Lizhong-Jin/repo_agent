import argparse

import pytest

from cli.settings import add_runtime_arguments, request_options
from llm import ConfigurationError, LLMRequest, Message
from llm.adapters import ADAPTERS
from llm.providers import get_provider
from llm.thinking import thinking_options


@pytest.mark.parametrize(
    "env_value,flags,expected",
    [
        ("", [], None),
        ("   ", [], None),
        ("https://custom.example/v1", ["--base-url", ""], None),
        ("", ["--base-url", "https://custom.example/v1"], "https://custom.example/v1"),
    ],
)
def test_cli_normalizes_empty_base_url(tmp_path, monkeypatch, env_value, flags, expected):
    from cli import main as cli
    from llm import LLMClient

    seen = []

    def make_client(config):
        seen.append(config.base_url)
        return LLMClient(config)

    monkeypatch.setenv("LLM_BASE_URL", env_value)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-key")
    monkeypatch.setattr(
        "sys.argv",
        [
            "repo-agent",
            "--sandbox",
            "local",
            "--root",
            str(tmp_path),
            "--provider",
            "deepseek",
            "--model",
            "mock",
            *flags,
        ],
    )
    monkeypatch.setattr(cli, "LLMClient", make_client)
    monkeypatch.setattr(cli, "run_interactive", lambda runtime, **kwargs: None)
    cli.main()  # Construct and close the real client without making any API requests.
    assert seen == [expected]


@pytest.mark.parametrize(
    "provider",
    [
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "qwen",
        "moonshot",
        "zhipu",
        "doubao",
        "minimax",
    ],
)
def test_auto_preserves_provider_defaults(provider):
    assert thinking_options(provider, "test") == {}


@pytest.mark.parametrize(
    "provider,model,settings,expected",
    [
        (
            "chatgpt",
            "test",
            {"mode": "enabled", "effort": "high"},
            {"reasoning": {"effort": "high"}},
        ),
        ("openai", "test", {"mode": "disabled"}, {"reasoning": {"effort": "none"}}),
        (
            "deepseek",
            "test",
            {"mode": "enabled", "effort": "high"},
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        ("kimi", "kimi-k2.5", {"mode": "disabled"}, {"thinking": {"type": "disabled"}}),
        ("glm", "test", {"mode": "enabled"}, {"thinking": {"type": "enabled"}}),
        ("doubao", "test", {"mode": "disabled"}, {"thinking": {"type": "disabled"}}),
        (
            "qwen",
            "test",
            {"mode": "enabled", "budget": 2048},
            {"enable_thinking": True, "thinking_budget": 2048},
        ),
        (
            "claude",
            "test",
            {"mode": "enabled", "budget": 2048},
            {"thinking": {"type": "enabled", "budget_tokens": 2048}},
        ),
        (
            "claude",
            "test",
            {"mode": "adaptive", "effort": "high"},
            {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
        ),
        (
            "gemini",
            "gemini-2.5-flash",
            {"mode": "disabled"},
            {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
        ),
        (
            "gemini",
            "gemini-2.5-flash",
            {"mode": "enabled", "budget": 2048},
            {"generationConfig": {"thinkingConfig": {"thinkingBudget": 2048}}},
        ),
        (
            "gemini",
            "gemini-3-pro-preview",
            {"mode": "enabled", "effort": "high"},
            {"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}},
        ),
    ],
)
def test_thinking_reaches_wire_format(provider, model, settings, expected):
    extra = thinking_options(provider, model, **settings)
    spec = get_provider(provider)
    body = ADAPTERS[spec.api_format](spec.name, model).encode(
        LLMRequest([Message("user", "task")], extra=extra)
    )
    for key, value in expected.items():
        if key == "generationConfig":
            assert body[key]["thinkingConfig"] == value["thinkingConfig"]
            assert body[key]["maxOutputTokens"] == 4096
        else:
            assert body[key] == value


@pytest.mark.parametrize(
    "provider,settings",
    [
        ("deepseek", {"mode": "bad"}),
        ("deepseek", {"mode": "disabled", "effort": "high"}),
        ("deepseek", {"budget": 2048}),
        ("openai", {"budget": 2048}),
        ("openai", {"mode": "adaptive"}),
        ("qwen", {"effort": "high"}),
        ("minimax", {"mode": "disabled"}),
        ("claude", {"mode": "enabled"}),
        ("claude", {"mode": "enabled", "budget": 100}),
        ("claude", {"mode": "enabled", "budget": 4096}),
        ("claude", {"mode": "adaptive", "budget": 2048}),
        ("gemini", {"effort": "high", "budget": 2048}),
    ],
)
def test_unsupported_thinking_settings_fail_explicitly(provider, settings):
    with pytest.raises(ConfigurationError):
        thinking_options(provider, "test", **settings)


def test_cli_overrides_environment_and_empty_optional_values(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "12")
    monkeypatch.setenv("LLM_TEMPERATURE", "")
    monkeypatch.setenv("LLM_TIMEOUT", "45")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--max-steps", "6"])
    assert args.max_steps == 6
    assert args.temperature is None
    assert args.timeout == 45.0


@pytest.mark.parametrize(
    "extra", ['{"thinking":{"type":"disabled"}}', '{"reasoning_effort":"low"}', "[]", "invalid"]
)
def test_extra_json_rejects_invalid_or_conflicting_options(extra):
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args(["--thinking", "enabled", "--extra-json", extra])
    args.provider = "deepseek"
    args.model = "test"
    with pytest.raises(ConfigurationError):
        request_options(args)


def test_stream_and_timeout_environment_overrides(monkeypatch):
    monkeypatch.setenv("LLM_STREAM", "false")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("LLM_WRITE_TIMEOUT", "25")
    monkeypatch.setenv("LLM_POOL_TIMEOUT", "8")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    args = parser.parse_args([])
    assert args.stream is False
    assert (args.connect_timeout, args.write_timeout, args.pool_timeout) == (7, 25, 8)
    args = parser.parse_args(["--stream", "--connect-timeout", "12"])
    assert args.stream is True
    assert args.connect_timeout == 12
    assert parser.parse_args(["--no-stream"]).stream is False


@pytest.mark.parametrize("value", ["{}", "[]", "null", "[1]", '[""]', "python -m pytest"])
def test_invalid_final_verification_command(value):
    from cli.settings import verification_command

    with pytest.raises(argparse.ArgumentTypeError):
        verification_command(value)


def test_final_verification_command_is_argv_not_shell():
    from cli.settings import verification_command

    assert verification_command("") is None
    assert verification_command('["python", "-m", "unittest"]') == ["python", "-m", "unittest"]


def test_context_window_cli_overrides_environment(monkeypatch):
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "131072")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    assert parser.parse_args([]).context_window == 131072
    assert parser.parse_args(["--context-window", "65536"]).context_window == 65536
    with pytest.raises(SystemExit):
        parser.parse_args(["--context-window", "0"])
