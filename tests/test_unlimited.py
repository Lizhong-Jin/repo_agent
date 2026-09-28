import argparse

import pytest
from test_runtime import RecordingTool, ScriptedLLM, reply

from agent import AgentRuntime
from cli.config_command import main as config_main
from cli.config_command import validate_value, validate_values
from cli.settings import add_runtime_arguments
from configuration.environment import load_configuration, read_config, user_config_path
from llm import ToolCall


def test_unlimited_runs_past_default_limits_and_stops_when_model_finishes():
    tool = RecordingTool()
    model = ScriptedLLM(
        [
            *[reply(calls=[ToolCall(str(i), "record", {"value": i})]) for i in range(75)],
            reply("finished"),
        ]
    )
    result = AgentRuntime(model, [tool], max_steps=0).run("long task")
    assert result.status == "completed" and result.steps == 76
    assert len(tool.seen) == 75 and len(model.requests) == 76
    assert model.requests[-1].max_output_tokens == 4096
    assert [c.step for c in result.stats.model_calls] == list(range(1, 77))


def test_unlimited_keeps_recovery_limit():
    model = ScriptedLLM([reply(f"part{i}", finish="length") for i in range(3)])
    result = AgentRuntime(model, max_steps=0).run("long task")
    assert result.status == "stopped" and result.resumable
    assert "自动恢复上限" in result.notice
    assert result.steps == len(model.requests) == 3


def test_unlimited_remains_cancellable_between_rounds():
    tool = RecordingTool()
    model = ScriptedLLM([reply(calls=[ToolCall(str(i), "record", {})]) for i in range(20)])
    runtime = AgentRuntime(model, [tool], max_steps=0)

    def check_cancelled():
        if len(tool.seen) >= 12:
            raise KeyboardInterrupt()

    runtime.check_cancelled = check_cancelled
    with pytest.raises(KeyboardInterrupt):
        runtime.run("long task")
    assert len(tool.seen) == len(model.requests) == 12
    assert runtime.last_stats.status == "interrupted"


def test_unlimited_does_not_change_positive_step_limit():
    model = ScriptedLLM([reply(calls=[ToolCall(str(i), "unknown", {})]) for i in range(3)])
    result = AgentRuntime(model, max_steps=3).run("bounded task")
    assert result.status == "max_steps" and result.steps == 3


def test_zero_roundtrips_through_config_commands_and_project_override(tmp_path, capsys):
    config_main(["set", "AGENT_MAX_STEPS", "0"])
    assert read_config(user_config_path())["AGENT_MAX_STEPS"] == "0"
    validate_values({"AGENT_MAX_STEPS": "0"})
    config_main(["show", "--root", str(tmp_path)])
    assert 'AGENT_MAX_STEPS = "0"' in capsys.readouterr().out
    values, _, _ = load_configuration(tmp_path)
    assert values["AGENT_MAX_STEPS"] == "0"
    (tmp_path / ".env").write_text("AGENT_MAX_STEPS=5\n")
    values, sources, _ = load_configuration(tmp_path)
    assert values["AGENT_MAX_STEPS"] == "5" and sources["AGENT_MAX_STEPS"] == "项目配置"


def test_cli_zero_and_environment_precedence(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "0")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    assert parser.parse_args([]).max_steps == 0
    assert parser.parse_args(["--max-steps", "5"]).max_steps == 5
    assert "0 表示轮数无上限" in parser.format_help()
    monkeypatch.setenv("AGENT_MAX_STEPS", "5")
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    assert parser.parse_args(["--max-steps", "0"]).max_steps == 0


@pytest.mark.parametrize("value", ["-1", "unlimited", "1.5"])
def test_invalid_step_limits_still_rejected(value):
    parser = argparse.ArgumentParser()
    add_runtime_arguments(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-steps", value])
    with pytest.raises(ValueError):
        validate_value("AGENT_MAX_STEPS", value)


def test_zero_is_not_an_unlimited_output_or_recovery_setting():
    with pytest.raises(ValueError):
        AgentRuntime(ScriptedLLM([]), max_steps=0, max_output_tokens=0)
    result = AgentRuntime(
        ScriptedLLM([reply("part", finish="length")]), max_steps=0, max_recoveries=0
    ).run("task")
    assert result.status == "stopped" and result.steps == 1
