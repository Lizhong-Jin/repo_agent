from types import SimpleNamespace

import pytest
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent import AgentRuntime
from agent.Tracing import ModelCallRecord, RunStats
from agent.transcript import Transcript
from cli.formatting import format_tokens
from cli.output import LiveOutput
from cli.session_status import SessionStatus
from cli.terminal.application import ConversationUI
from cli.terminal.widgets import ConversationLexer
from llm import Usage


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "未返回"),
        (0, "0"),
        (999, "999"),
        (1000, "1k"),
        (6687, "6.69k"),
        (48304, "48.3k"),
        (999994, "999.99k"),
        (999995, "1M"),
        (1_250_000, "1.25M"),
    ],
)
def test_token_units_round_without_changing_accounting(value, expected):
    assert format_tokens(value) == expected


def test_agent_metadata_style_is_based_on_origin_including_after_fold():
    transcript = Transcript()
    lexer = ConversationLexer({}, set(), transcript.agent_lines)
    record = ModelCallRecord(1, usage=Usage(6687, 48304, reasoning_tokens=44963))
    live = LiveOutput(
        lambda *args, **kw: transcript.append(" ".join(args) + kw.get("end", "\n")),
        write_meta=lambda *args, **kw: transcript.append(
            " ".join(args) + kw.get("end", "\n"), kind="agent"
        ),
    )
    # Even if the model writes a metadata-looking prefix, it remains model content.
    live("text", "[用量：模型写的文字]", 1.0, record)
    live("usage", "", 2.0, record)
    transcript.thinking("thinking_start", "", 1, 2.0)
    transcript.thinking("thinking_delta", "思考片段", 1, 3.0)
    transcript.thinking("thinking_end", "", 1, 4.0)
    transcript.append("[模型 #1：模型写的文字]\n")
    for mode in ("collapsed", "expanded", "hidden"):
        text, _, _ = transcript.render(mode)
        doc = Document(text)
        lines = lexer.lex_document(doc)
        for i, line in enumerate(doc.lines):
            if "模型写的文字" in line:
                assert lines(i)[0][0] == ""
            if "其中思考" in line:
                assert lines(i)[0][0] == "class:agent"
                assert "6.69k" in line and "48.3k" in line and "44.96k" in line
    assert record.usage.output_tokens == 48304


def test_compact_footer_retains_directory_usage_context_and_unknowns(tmp_path):
    status = SessionStatus(tmp_path, context_window=1_000_000)
    stats = RunStats(1)
    stats.model_calls.append(
        ModelCallRecord(
            1,
            usage=Usage(1_200_000, 50_000, cached_input_tokens=900_000),
        )
    )
    status("model_end", stats)
    with create_pipe_input() as pipe:
        ui = ConversationUI(
            AgentRuntime(object()),
            status=status,
            models=SimpleNamespace(describe=lambda: "模型：zhipu / glm-5.3（/model 切换）"),
            terminal_input=pipe,
            terminal_output=DummyOutput(),
        )
        assert len(ui.footer_text.splitlines()) == 3
        assert "输入 1.2M · 输出 50k" in ui.footer_text
        assert "上下文 ≈1.25M / 1M · ≈125.0% · 缓存命中 75.0%" in ui.footer_text
        assert str(tmp_path.resolve()) in ui.footer_text
        assert status.totals == {"input_tokens": 1_200_000, "output_tokens": 50_000}
        status.reset_context()
        ui.refresh_footer()
        assert "占用未知" in ui.footer_text and "%" not in ui.footer_text
        assert "缓存命中 未知" in ui.footer_text
