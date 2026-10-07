"""Keyboard actions for the full-screen view; no model execution here."""

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from llm import LLMError

from ..shortcuts import shortcut_label
from .report_page import report_bindings


def create_bindings(ui):
    keys = KeyBindings()
    chatting = Condition(lambda: ui.report_page is None)
    choosing_model = Condition(
        lambda: ui.model_wizard is not None and ui.model_wizard.stage == "model"
    )

    def search_model(_):
        if choosing_model() and ui.model_picker:
            ui.model_picker.search(ui.editor.text)

    ui.editor.buffer.on_text_changed += search_model

    @keys.add("up", filter=choosing_model)
    def model_up(event):
        ui.model_picker.move(-1)

    @keys.add("down", filter=choosing_model)
    def model_down(event):
        ui.model_picker.move(1)

    @keys.add("enter", filter=chatting)
    def submit(event):
        ui.submit()

    @keys.add("escape", "enter", filter=chatting)
    def newline(event):
        ui.editor.buffer.insert_text("\n")

    @keys.add("s-tab", filter=chatting)
    def thinking(event):
        if ui.model_wizard:
            return
        if ui.busy:
            ui.phase = "任务运行中；结束后可切换思考设置"
        elif ui.thinking:
            try:
                ui.controller.require_idle_configuration()
                ui.thinking.cycle()
                ui.refresh_footer()
            except (ValueError, OSError, LLMError) as error:
                ui.append(f"\n设置未变更：{error}\n")

    @keys.add("f3")
    def report(event):
        if ui.report_page:
            ui.report_page.close()
        else:
            ui.open_report()

    @keys.add("f2", filter=chatting)
    def rename_session(event):
        if ui.conversation and not ui.busy and not ui.model_wizard and not ui.renaming:
            ui.rename_draft = ui.editor.text
            ui.renaming = True
            ui.editor.text = ui.conversation.store.catalog.get(ui.conversation.store.id)["name"]
            ui.editor.buffer.cursor_position = len(ui.editor.text)
            ui.phase = f"会话改名 · {shortcut_label('Enter')} 保存 · Ctrl+C 取消"

    @keys.add("c-t", filter=chatting)
    def toggle_thinking(event):
        if ui.model_wizard:
            return
        try:
            ui.display.toggle()
            ui.flush_text()
            ui.render_transcript()
            ui.refresh_footer()
        except (ValueError, OSError, LLMError) as error:
            ui.append(f"\n设置未变更：{error}\n")

    @keys.add("c-c")
    def interrupt(event):
        if ui.report_page and ui.report_page.reviewing:
            ui.report_page.cancel_review()
            return
        if ui.report_page and not ui.busy:
            ui.report_page.close()
            return
        if ui.renaming:
            ui.renaming = False
            ui.editor.text = ui.rename_draft
            ui.phase = "改名已取消"
            return
        if ui.model_wizard:
            ui.cancel_model()
            return
        if ui.busy:
            ui.stop_task()
            ui.phase = "正在停止；正在取消请求并等待安全收尾…"
        else:
            ui.editor.text = ""
            ui.phase = "输入已清空；Ctrl+D 退出"

    @keys.add("c-d")
    def exit_app(event):
        if ui.report_page:
            ui.report_page.close()
            return
        if ui.renaming:
            interrupt(event)
            return
        if ui.model_wizard:
            ui.cancel_model()
            return
        if ui.busy:
            ui.stop_task()
            ui.phase = "正在停止；结束后再次 Ctrl+D 退出"
        elif not ui.editor.text:
            ui.app.exit()

    @keys.add("pageup", filter=chatting)
    def up(event):
        if choosing_model():
            ui.model_picker.move(-ui.model_picker.page_size)
        else:
            ui.scroll_history(-ui.history_page_size())

    @keys.add("pagedown", filter=chatting)
    def down(event):
        if choosing_model():
            ui.model_picker.move(ui.model_picker.page_size)
        else:
            ui.scroll_history(ui.history_page_size())

    @keys.add("c-end", filter=chatting)
    @keys.add("escape", "g", filter=chatting)
    def end(event):
        ui.follow_latest()

    report_bindings(ui, keys)
    return keys
