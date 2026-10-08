"""Exercise report navigation in a running TUI with real saved reports/receipts."""

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_task_reports import begin, finish, record
from test_tui import until
from test_verification_plan import command_item, declare, execute_check

from agent.task_reports import ReportStore, load_report
from cli.terminal.application import ConversationUI
from llm import ToolCall
from tools.filesystem import WriteFileTool


class Screen(DummyOutput):
    def __init__(self, columns=100):
        self.columns = columns

    def get_size(self):
        return Size(rows=32, columns=self.columns)


def saved_report(c):
    capture = begin(c)
    call = ToolCall(
        "write", "write_file", {"path": "a.py", "content": "agent\n", "overwrite": True}
    )
    record(c, capture, call, lambda: WriteFileTool(c.store.project).execute(call.arguments))
    (c.store.project / "a.py").write_text("concurrent edit\n")
    declare(c, capture, [command_item(), command_item("second")])
    execute_check(c, capture, code=1)
    report = finish(capture)
    c.queue.finish(c.queue.running, "completed", {})
    return report


@asynccontextmanager
async def running(c, width=100):
    with create_pipe_input() as pipe:
        ui = ConversationUI(
            c.runtime, conversation=c, terminal_input=pipe, terminal_output=Screen(width)
        )
        task = asyncio.create_task(ui.run_async())
        try:
            await until(lambda: ui.app.is_running)
            yield ui, pipe
        finally:
            if ui.report_page:
                # Do not close the application with a pending review write.
                await until(lambda: not ui.report_page.saving)
                ui.report_page.reviewing = None
                ui.report_page.close()
            if not task.done():
                ui.app.exit()
            await asyncio.wait_for(task, 3)


def rendered(ui):
    screen = ui.app.renderer._last_screen
    return "\n".join(
        "".join(cell.char for _, cell in sorted(row.items()))
        for _, row in sorted(screen.data_buffer.items())
    )


@pytest.mark.parametrize("width", [40, 60, 120])
def test_report_page_keyboard_navigation_and_draft_preserved(open_conversation, width):
    c = open_conversation()
    saved_report(c)

    async def run():
        async with running(c, width) as (ui, pipe):
            ui.editor.text = "尚未发送的草稿"
            original = ui.transcript
            pipe.send_text("\x1b[13~")  # F3
            await until(lambda: ui.report_page and ui.report_page.report)
            page = ui.report_page
            await until(lambda: "任务报告" in rendered(ui))
            assert "返回" in rendered(ui)
            assert "Esc 返回" in rendered(ui)
            page.select_section(1)
            assert "+concurrent edit" in page.detail.text
            assert "修改归属未确认" in page.detail.text
            page.select_section(2)
            assert "+agent" in page.detail.text and "changed_after" in page.detail.text
            page.read_evidence()
            await until(lambda: "账本引用" in page.detail.text)
            assert "success" in page.detail.text
            pipe.send_text("\x1b[5~")  # Report paging must not scroll chat.
            await asyncio.sleep(0.05)
            assert ui.follow
            pipe.send_text("\x1b")
            await until(lambda: ui.report_page is None)
            assert ui.editor.text == "尚未发送的草稿"
            assert ui.app.layout.has_focus(ui.editor)
            assert ui.transcript == original
            assert not c.runtime.llm.requests

    asyncio.run(run())


def test_report_command_task_navigation_and_empty_error_recovery(open_conversation):
    c = open_conversation()
    saved_report(c)
    saved_report(c)

    async def run():
        async with running(c) as (ui, pipe):
            original = ui.transcript
            pipe.send_text("/report 1 --diff\r")
            await until(lambda: ui.report_page and ui.report_page.report)
            page = ui.report_page
            assert page.section == 1 and page.report["task_number"] == 1
            page.move_task(1)
            await until(lambda: page.report and page.report["task_number"] == 2)
            page.load("999")
            await until(lambda: not page.loading)
            assert page.report is None and "没有对应任务报告" in page.message
            page.move_task(-1)
            await until(lambda: page.report and page.report["task_number"] == 2)
            assert ui.transcript == original
            assert not c.runtime.llm.requests

    asyncio.run(run())


def test_no_report_is_not_fabricated(open_conversation):
    c = open_conversation()

    async def run():
        async with running(c) as (ui, pipe):
            ui.open_report()
            page = ui.report_page
            await until(lambda: not page.loading)
            assert page.report is None
            assert "没有对应任务报告" in page.message
            assert not c.runtime.llm.requests

    asyncio.run(run())


def test_incomplete_report_can_be_opened_while_busy_but_not_accepted(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])

    async def run():
        async with running(c) as (ui, pipe):
            ui.busy = True
            pipe.send_text("/report\r")
            await until(lambda: ui.report_page and ui.report_page.report)
            page = ui.report_page
            assert page.report["state"] == "incomplete"
            page.select_section(3)
            page.begin_review("accepted")
            assert page.reviewing is None and "尚未完整保存" in page.message
            finish(capture)
            page.refresh()
            await until(lambda: page.report and page.report["state"] == "completed")
            assert c.runtime.llm.requests == []
            ui.busy = False

    asyncio.run(run())


@pytest.mark.parametrize("defer_render", [False, True])
def test_review_form_saves_without_overwriting_failed_command(
    open_conversation, monkeypatch, defer_render
):
    c = open_conversation()
    saved_report(c)

    async def run():
        async with running(c) as (ui, pipe):
            ui.open_report(section=3)
            page = ui.report_page
            await until(lambda: page.report is not None)
            await until(lambda: "检查失败" in rendered(ui))
            # Keyboard input can arrive before the newly opened form is rendered.
            # Tab must work even when the renderer still shows the report browser.
            if defer_render:
                monkeypatch.setattr(ui.app.renderer, "render", lambda *args, **kwargs: None)
            page.begin_review("accepted")
            page.save_review()
            assert "请填写" in page.message
            pipe.send_text("人工复查通过")
            await until(lambda: page.note.text == "人工复查通过")
            pipe.send_text("\x1b[Z")  # Shift+Tab wraps to cancel, even before rendering.
            await until(lambda: ui.app.layout.has_focus(page.cancel_button))
            pipe.send_text("\t")
            await until(lambda: ui.app.layout.has_focus(page.note))
            pipe.send_text("\t\r")  # Tab to save; Enter must activate the button, not submit chat.
            await until(
                lambda: (
                    page.report
                    and page.report["verification_items"][0]["acceptance"]["state"] == "accepted"
                )
            )
            assert page.report["verification_items"][0]["state"] == "failed"
            assert "检查失败" in page.detail.text and "用户确认通过" in page.detail.text
            assert not c.runtime.llm.requests
            page.move(1)
            page.begin_review("pending")
            page.note.text = "仍待复查"
            page.save_review()
            await until(lambda: not page.saving and not page.loading)
            assert page.selected[2]["id"] == "second"
            assert page.selected[2]["acceptance"]["note"] == "仍待复查"
            page.move(-1)
            page.begin_review("rejected")
            page.note.text = "取消时不应保存"
            pipe.send_text("\x1b")
            await until(lambda: page.reviewing is None)
            assert ui.report_page is page
            assert (
                load_report(c.store, c.ledger)["verification_items"][0]["acceptance"]["state"]
                == "accepted"
            )

    asyncio.run(run())


def test_changed_report_rejects_stale_acceptance(open_conversation):
    c = open_conversation()
    saved_report(c)

    async def run():
        async with running(c) as (ui, pipe):
            ui.open_report(section=3)
            page = ui.report_page
            await until(lambda: page.report is not None)
            page.begin_review("accepted")
            report = ReportStore(c.store).get()
            report["outcome"]["notice"] = "revised"
            ReportStore(c.store).save(report)
            page.note.text = "reviewing the old report"
            page.save_review()
            await until(lambda: not page.saving)
            assert "报告内容已变化" in page.message
            assert page.reviewing is not None
            assert (
                load_report(c.store, c.ledger)["verification_items"][0]["acceptance"]["state"]
                == "pending"
            )

    asyncio.run(run())


def test_slow_report_read_does_not_block_ui_or_replace_newer_selection(
    open_conversation, monkeypatch
):
    c = open_conversation()
    saved_report(c)
    saved_report(c)
    started, release = threading.Event(), threading.Event()

    def delayed(store, ledger, selector):
        if selector == "1":
            started.set()
            assert release.wait(3)
        return load_report(store, ledger, selector)

    monkeypatch.setattr("cli.terminal.report_page.load_report", delayed)

    async def run():
        async with running(c) as (ui, pipe):
            ui.open_report("1")
            page = ui.report_page
            await until(started.is_set)
            page.load("2")
            await until(lambda: page.report is not None)
            assert page.report["task_number"] == 2
            release.set()
            await until(lambda: not page.jobs)
            assert page.report["task_number"] == 2
            ui.editor.text = "draft"
            page.close()
            assert ui.editor.text == "draft"

    asyncio.run(run())


def test_control_characters_and_long_output_are_bounded():
    from cli.terminal.report_page import DISPLAY_LIMIT, bounded

    value = bounded("\x1b]52;malicious\x07" + "x" * (DISPLAY_LIMIT + 1))
    assert "\x1b" not in value and "\x07" not in value
    assert "显示已截断" in value
    assert len(value) < DISPLAY_LIMIT + 200


def test_closing_page_ignores_late_read_and_preserves_editor(open_conversation, monkeypatch):
    c = open_conversation()
    saved_report(c)
    started, release = threading.Event(), threading.Event()

    def delayed(store, ledger, selector):
        started.set()
        assert release.wait(3)
        return load_report(store, ledger, selector)

    monkeypatch.setattr("cli.terminal.report_page.load_report", delayed)

    async def run():
        async with running(c) as (ui, pipe):
            ui.open_report()
            page = ui.report_page
            await until(started.is_set)
            page.close()
            pipe.send_text("新的草稿")
            await until(lambda: ui.editor.text == "新的草稿")
            release.set()
            await until(lambda: not page.jobs)
            assert ui.report_page is None
            assert ui.app.layout.has_focus(ui.editor)
            assert not c.runtime.llm.requests

    asyncio.run(run())
