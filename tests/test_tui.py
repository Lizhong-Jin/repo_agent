import asyncio
import time

from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_live import control

from agent import AgentRuntime
from agent.skills import SkillRegistry
from cli.session_status import SessionStatus
from cli.terminal.application import ConversationUI
from llm import LLMResponse, Message, Usage


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 3)


class StreamingModel:
    def __init__(self, endless=False):
        self.endless = endless
        self.requests = []

    def generate_with_events(self, request, callback):
        self.requests.append(request)
        callback("first_text", "", 0.01)
        callback("text", "正在回答", 0.01)
        try:
            for _ in range(1000 if self.endless else 20):
                time.sleep(0.005)
                callback("first_data", "", 0.02)
            return LLMResponse(
                provider="test",
                model="m",
                message=Message("assistant", "正在回答"),
                finish_reason="stop",
                usage=Usage(10, 5),
            )
        finally:
            callback("end", "", 0.2)


def test_skills_list_and_explicit_load_in_full_terminal(tmp_path):
    async def run():
        model = StreamingModel()
        runtime = AgentRuntime(model, skills=SkillRegistry(tmp_path))
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("/skills\r")
            await until(lambda: "$debug-and-fix" in ui.transcript)
            assert not model.requests and not ui.busy
            pipe.send_text("$debug-and-fix 分析报错\r")
            await until(lambda: "[已加载技能：debug-and-fix]" in ui.transcript)
            await until(lambda: not ui.busy)
            assert len(model.requests) == 1
            assert "复现与定位" in model.requests[0].messages[-1].content
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_full_application_stream_draft_footer_and_exit(tmp_path):
    async def run():
        model = StreamingModel()
        runtime = AgentRuntime(model)
        c = control()
        c.model = "glm-5.2"
        c.runtime = runtime
        status = SessionStatus(tmp_path)
        runtime.on_event = status
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                runtime,
                thinking=c,
                status=status,
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("hello\r")
            await until(lambda: "正在回答" in ui.transcript)
            assert ui.busy
            pipe.send_text("下一条")
            await until(lambda: ui.editor.text == "下一条")
            await until(lambda: not ui.busy)
            assert "输入 10 · 输出 5" in ui.footer_text
            assert str(tmp_path) in ui.footer_text
            assert ui.transcript.count("正在回答") == 1
            pipe.send_text("\x1b[Z")
            await until(lambda: c.current["mode"] == "disabled")
            assert ui.editor.text == "下一条"
            pipe.send_text("\x03")
            await until(lambda: not ui.editor.text)
            pipe.send_text("/clear\r")
            await until(lambda: "上下文已清空" in ui.transcript)
            assert not ui.history and status.totals["input_tokens"] == 10
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)
        assert runtime.on_event is status

    asyncio.run(run())


def test_cancel_stream_clears_history_keeps_draft_and_waits(tmp_path):
    async def run():
        runtime = AgentRuntime(StreamingModel(endless=True))
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                runtime,
                status=SessionStatus(tmp_path),
                terminal_input=pipe,
                terminal_output=DummyOutput(),
            )
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("start\r")
            await until(lambda: "正在回答" in ui.transcript)
            pipe.send_text("draft\x03")
            await until(lambda: not ui.busy)
            assert "已中断" in ui.transcript
            assert not ui.history and ui.editor.text == "draft"
            assert runtime.last_stats.status == "interrupted"
            pipe.send_text("\x03")
            await until(lambda: not ui.editor.text)
            pipe.send_text("\x04")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_multiline_input_and_scroll_do_not_submit(tmp_path):
    async def run():
        model = StreamingModel()
        runtime = AgentRuntime(model)
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.app.is_running)
            pipe.send_text("one\x1b\rtwo")
            await until(lambda: ui.editor.text == "one\ntwo")
            assert not model.requests
            ui.append("history\n" * 50)
            pipe.send_text("\x1b[5~")
            await until(lambda: not ui.follow)
            pipe.send_text("\x03")
            await until(lambda: not ui.editor.text)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


class SmallTerminal(DummyOutput):
    def get_size(self):
        return Size(rows=24, columns=60)


def test_mouse_scroll_stays_on_history_during_output_and_keeps_editor_focus():
    async def run():
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                AgentRuntime(StreamingModel()),
                terminal_input=pipe,
                terminal_output=SmallTerminal(),
            )
            ui.append("".join(f"history {i}\n" for i in range(100)))
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.chat.window.render_info is not None)
            window = ui.chat.window
            original = window.vertical_scroll
            # Actual terminal SGR mouse event inside the transcript, not a
            # direct callback: verifies mouse routing with focus in the editor.
            pipe.send_text("\x1b[<64;5;5M")
            await until(lambda: not ui.follow and window.vertical_scroll < original)
            assert window.vertical_scroll == original - 3
            position = (window.vertical_scroll, window.vertical_scroll_2)
            ui.append("new streamed text\n" * 5)
            pipe.send_text("draft")
            await until(lambda: ui.editor.text == "draft")
            assert (window.vertical_scroll, window.vertical_scroll_2) == position
            assert ui.app.layout.has_focus(ui.editor)
            assert "正在查看历史" in ui.phase_text()
            # Page keys move a viewport, not a fixed number of logical lines.
            page_size = ui.history_page_size()
            pipe.send_text("\x1b[5~")
            await until(lambda: window.vertical_scroll == position[0] - page_size)
            pipe.send_text("\x1b[6~")
            await until(lambda: window.vertical_scroll == position[0])
            pipe.send_text("\x1bg")
            await until(lambda: ui.follow)
            await until(lambda: window.vertical_scroll > original)
            assert "正在查看历史" not in ui.phase_text()
            assert ui.editor.text == "draft"
            pipe.send_text("\x03\x04")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())


def test_mouse_can_scroll_inside_single_wrapped_paragraph_and_resume_at_bottom():
    async def run():
        with create_pipe_input() as pipe:
            ui = ConversationUI(
                AgentRuntime(StreamingModel()),
                terminal_input=pipe,
                terminal_output=SmallTerminal(),
            )
            ui.append("中文长段落 mixed text " * 300)
            task = asyncio.create_task(ui.run_async())
            await until(lambda: ui.chat.window.vertical_scroll_2 > 10)
            window = ui.chat.window
            line, offset = window.vertical_scroll, window.vertical_scroll_2
            pipe.send_text("\x1b[<64;5;5M")
            await until(lambda: not ui.follow and window.vertical_scroll_2 == offset - 3)
            assert window.vertical_scroll == line
            previous_render = window.render_info
            ui.append("继续生成" * 100)
            await until(lambda: window.render_info is not previous_render)
            assert window.vertical_scroll_2 == offset - 3
            # A large downward movement clamps to the bottom and restores follow.
            ui.scroll_history(10000)
            await until(lambda: ui.follow and window.vertical_scroll_2 > offset)
            pipe.send_text("\x04")
            await asyncio.wait_for(task, 3)

    asyncio.run(run())
