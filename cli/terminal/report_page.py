"""A report browser over persisted evidence, independent of the chat transcript."""

import asyncio
import json
from copy import copy

from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import ConditionalContainer, DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.widgets import Button, Frame, TextArea

from agent.execution_ledger import ExecutionLedger
from agent.report_reviews import record_review
from agent.task_reports import load_report, render_report
from agent.transcript import display_text
from agent.verification import render_verification

SECTIONS = ("总览", "文件变化", "工具操作", "验证清单", "执行记录")
DISPLAY_LIMIT = 100000
LABELS = {
    "added": "新增",
    "deleted": "删除",
    "modified": "修改",
    "unknown": "未确认",
    "command_succeeded": "命令成功",
    "failed": "失败",
    "timed_out": "超时",
    "not_executed": "未执行",
    "passed": "命令通过",
    "not_run": "未执行",
    "manual_pending": "待人工检查",
    "needs_recheck": "需重新验证",
    "matches": "最终内容一致",
    "changed_after": "之后存在其他变化",
}


class ReportLexer(Lexer):
    def lex_document(self, document):
        def line(number):
            text = document.lines[number]
            style = "#88bb88" if text.startswith("+") else "#ee9999" if text.startswith("-") else ""
            return [(style, text)]

        return line


def bounded(text):
    suffix = "\n[显示已截断；使用 sessions report --json 导出报告，原始结果可按引用回查]"
    return display_text(text[:DISPLAY_LIMIT]) + (suffix if len(text) > DISPLAY_LIMIT else "")


def pretty(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


class ReportPage:
    def __init__(self, ui):
        self.ui = ui
        # Freeze the session identity: an outstanding read must not follow a later /new.
        self.store = copy(ui.conversation.store)
        self.ledger = ExecutionLedger(self.store)
        self.session_title = ui.session_title
        self.report = None
        self.selector = "latest"
        self.section = 0
        self.index = 0
        self.entries = []
        self.loading = False
        self.saving = False
        self.reviewing = None
        self.message = ""
        self.generation = 0
        self.jobs = set()
        self.detail = TextArea(read_only=True, scrollbar=True, wrap_lines=True, lexer=ReportLexer())
        self.note = TextArea(height=3, multiline=True, prompt="验收备注> ")
        self.menu = Window(
            FormattedTextControl(
                self.fragments,
                focusable=True,
                key_bindings=self.menu_bindings(),
                get_cursor_position=lambda: Point(x=0, y=self.index),
            ),
            wrap_lines=False,
        )
        self.container = self.build()

    def build(self):
        toolbar_buttons = [
            Button("上一任务", lambda: self.move_task(-1)),
            Button("下一任务", lambda: self.move_task(1)),
            Button("刷新", self.refresh, width=8),
            Button("返回", self.close, width=8),
        ]
        toolbar = DynamicContainer(
            lambda: (
                VSplit(toolbar_buttons, padding=1)
                if self.width >= 54
                else HSplit([VSplit(toolbar_buttons[:2]), VSplit(toolbar_buttons[2:])])
            )
        )
        tabs = [
            Button(label, lambda i=i: self.select_section(i), width=12)
            for i, label in enumerate(SECTIONS)
        ]
        wide = VSplit([Frame(self.menu, title="条目", width=32), Frame(self.detail, title="详情")])
        narrow = HSplit(
            [Frame(self.menu, title="条目", height=7), Frame(self.detail, title="详情")]
        )
        action_buttons = [
            Button("原始结果", self.read_evidence),
            Button("验收通过", lambda: self.begin_review("accepted")),
            Button("未通过", lambda: self.begin_review("rejected")),
            Button("待验收", lambda: self.begin_review("pending")),
        ]
        actions = DynamicContainer(
            lambda: (
                VSplit(action_buttons)
                if self.width >= 54
                else HSplit([VSplit(action_buttons[:2]), VSplit(action_buttons[2:])])
            )
        )
        return HSplit(
            [
                Window(FormattedTextControl(self.title), height=2, style="class:header"),
                toolbar,
                DynamicContainer(
                    lambda: (
                        VSplit(tabs)
                        if self.width >= 70
                        else HSplit([VSplit(tabs[:3]), VSplit(tabs[3:])])
                    )
                ),
                DynamicContainer(lambda: wide if self.width >= 90 else narrow),
                ConditionalContainer(actions, filter=Condition(lambda: not self.reviewing)),
                ConditionalContainer(
                    HSplit(
                        [
                            self.note,
                            VSplit(
                                [
                                    Button("保存意见", self.save_review),
                                    Button("取消", self.cancel_review),
                                ]
                            ),
                        ]
                    ),
                    filter=Condition(lambda: self.reviewing is not None),
                ),
                Window(
                    FormattedTextControl(lambda: bounded(self.message)), height=2, wrap_lines=True
                ),
                Window(
                    FormattedTextControl(
                        lambda: (
                            "Tab 焦点 · ↑↓ 选择 · Esc 返回\nPgUp/PgDn 详情 · Ctrl+C 停止"
                            if self.width < 70
                            else "Tab 切换焦点 · ↑↓ 选条目 · PgUp/PgDn 详情 · Esc 返回\n"
                            "Ctrl+C 停止运行任务 · 报告页不向模型发送内容"
                        )
                    ),
                    height=2,
                    style="class:hint",
                ),
            ]
        )

    @property
    def width(self):
        return self.ui.app.output.get_size().columns

    @property
    def active(self):
        return self.ui.report_page is self

    def title(self):
        state = " · 读取中…" if self.loading else ""
        task = f"#{self.report['task_number']}" if self.report else str(self.selector)
        return bounded(
            f"任务报告 · {self.session_title} · 任务 {task}{state}\n"
            f"{SECTIONS[self.section]} · {self.ui.phase}"
        )

    def set_detail(self, text):
        self.detail.buffer.set_document(Document(bounded(text), 0), bypass_readonly=True)
        self.ui.app.invalidate()

    def schedule(self, coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    def refresh(self):
        self.load(self.selector, restore=(self.section, self.index))

    def load(self, selector="latest", *, section=None, restore=None):
        if self.saving or self.reviewing:
            self.message = "请先保存或取消验收意见"
            return
        self.selector = selector
        if section is not None:
            self.section = section
        self.generation += 1
        generation = self.generation
        self.loading = True
        self.report = None
        self.entries = []
        self.index = 0
        self.message = "正在读取已保存报告…"
        self.set_detail("")

        async def read():
            try:
                report = await asyncio.to_thread(load_report, self.store, self.ledger, selector)
            except Exception as error:
                if self.active and generation == self.generation:
                    self.message = f"报告未能打开：{error}；可选择其他任务或刷新。"
                    self.set_detail("没有可显示的报告；不会用当前文件补造历史状态。")
            else:
                if self.active and generation == self.generation:
                    self.report = report
                    self.selector = str(report["task_number"])
                    self.select_section(self.section)
                    if restore and restore[0] == self.section:
                        self.move(restore[1])
                    self.message = (
                        "结束快照尚未保存，最终差异未确认；任务结束后可刷新。"
                        if report["state"] == "incomplete"
                        else "历史报告；显示保存时的状态。人工验收与命令结果分别记录。"
                    )
            finally:
                if self.active and generation == self.generation:
                    self.loading = False
                    self.ui.app.invalidate()

        self.schedule(read())

    def move_task(self, direction):
        numbers = sorted(
            t["number"] for t in self.ui.controller.queue.data["tasks"] if t["attempts"]
        )
        current = (
            int(self.selector) if str(self.selector).isdigit() else (numbers[-1] if numbers else 0)
        )
        candidates = [n for n in numbers if (n - current) * direction > 0]
        if candidates:
            self.load(str(min(candidates) if direction > 0 else max(candidates)))
        else:
            self.message = "没有更早的任务" if direction < 0 else "没有更新的任务"

    def select_section(self, section):
        if self.reviewing or self.saving:
            self.message = "请先保存或取消验收意见"
            return
        self.section, self.index = section, 0
        report = self.report or {}
        if section == 0:
            self.entries = [("交付概况与限制", "summary", report)] if report else []
        elif section == 1:
            self.entries = [
                (f"{LABELS.get(c['kind'], c['kind'])} · {c['path']}", "change", c)
                for c in report.get("changes", [])
            ]
            self.entries += [
                ("宿主 · " + c["path"], "host_change", c) for c in report.get("host_changes", [])
            ]
        elif section == 2:
            self.entries = [
                (f"{c['tool']} · {c['path']}", "operation", c) for c in report.get("operations", [])
            ]
        elif section == 3:
            self.entries = [
                (f"{LABELS.get(c['state'], c['state'])} · [{c['id']}] {c['title']}", "check", c)
                for c in report.get("verification_items", [])
            ]
        else:
            self.entries = [
                (f"{c['tool']} · {LABELS.get(c['state'], c['state'])}", "execution", c)
                for c in report.get("executions", [])
            ]
            self.entries += [
                (f"失败/待核实 · {c['tool']}", "failure", c) for c in report.get("failures", [])
            ]
            if report.get("host_verification"):
                self.entries.append(
                    ("宿主回写验证", "host_verification", report["host_verification"])
                )
        self.show_selected()

    @property
    def selected(self):
        return self.entries[self.index] if self.entries else None

    def show_selected(self):
        if not self.selected:
            self.set_detail("此分类没有已保存的条目；不代表所有检查已通过。")
            return
        _, kind, item = self.selected
        if kind == "summary":
            text = (
                render_report(item)
                + "\n\n"
                + pretty(
                    {
                        "开始": item.get("started_at"),
                        "结束": item.get("finished_at"),
                        "执行尝试": item["attempt_id"],
                        "重试来源": item.get("retry_of"),
                        "续接来源": item.get("continuation_of"),
                        "性能": item.get("metrics"),
                        "快照范围": {
                            k: {
                                field: item[k].get(field)
                                for field in ("root", "complete", "issues", "excluded", "policy")
                            }
                            for k in ("baseline", "final", "host_baseline", "host_final")
                            if k in item
                        },
                    }
                )
            )
        elif kind == "check":
            text = render_verification([item]) + "\n\n执行记录与验收历史：\n" + pretty(item)
        elif kind in {"change", "host_change", "operation"}:
            label = (
                "已记录的工具操作；不代表工具独占修改了整个文件。\n"
                + LABELS.get(item.get("final_state"), "最终状态未确认")
                if kind == "operation"
                else "任务期间观察到的变化；修改归属未确认。"
            )
            text = label + "\n\n" + (item.get("diff") or "无可显示的文本差异；详见下方记录。")
            text += "\n\n" + pretty({k: v for k, v in item.items() if k != "diff"})
        else:
            text = "实际执行记录；命令成功不等于需求全部满足。\n" + pretty(item)
        self.set_detail(text)

    def move(self, amount):
        if self.reviewing:
            return
        self.index = max(0, min(self.index + amount, len(self.entries) - 1))
        self.show_selected()

    def fragments(self):
        if not self.entries:
            return [("", "暂无条目")]

        def click(index):
            def handle(event):
                if self.reviewing:
                    return NotImplemented
                if event.event_type == MouseEventType.MOUSE_UP:
                    self.ui.app.layout.focus(self.menu)
                    self.index = index
                    self.show_selected()
                elif event.event_type == MouseEventType.SCROLL_UP:
                    self.move(-1)
                elif event.event_type == MouseEventType.SCROLL_DOWN:
                    self.move(1)
                else:
                    return NotImplemented

            return handle

        return [
            (
                "reverse" if i == self.index else "",
                bounded(label).replace("\n", " ") + "\n",
                click(i),
            )
            for i, (label, _, _) in enumerate(self.entries)
        ]

    def menu_bindings(self):
        keys = KeyBindings()
        keys.add("up")(lambda _: self.move(-1))
        keys.add("down")(lambda _: self.move(1))
        keys.add("enter")(lambda _: self.ui.app.layout.focus(self.detail))
        return keys

    def read_evidence(self):
        if not self.selected or self.loading or self.reviewing:
            return
        _, _, item = self.selected
        ref = item.get("ledger", {}).get("result_ref")
        if not ref and item.get("attempts"):
            ref = item["attempts"][-1]["ledger"].get("result_ref")
        if not ref:
            self.message = "当前条目没有已保存的原始工具结果引用"
            return
        selected, generation = self.selected, self.generation

        def read_text():
            result = self.ledger.read_result(ref)
            return bounded(
                f"账本引用：{ref}\n\n" + (pretty(result["payload"]) if result else "原始结果不可用")
            )

        async def read():
            try:
                text = await asyncio.to_thread(read_text)
            except Exception as error:
                text = f"原始结果未能读取：{error}"
            if (
                self.active
                and generation == self.generation
                and self.selected is selected
                and not self.reviewing
            ):
                self.set_detail(text)
                self.ui.app.layout.focus(self.detail)

        self.schedule(read())

    def begin_review(self, state):
        if self.loading or self.saving or self.reviewing:
            return
        if not self.selected or self.selected[1] != "check":
            self.message = "请先在“验证清单”选择一个检查项目"
            return
        if self.report["state"] == "incomplete":
            self.message = "任务报告尚未完整保存，不能提交验收结论"
            return
        self.reviewing = (self.selected[2]["id"], state)
        self.note.text = ""
        labels = {"accepted": "通过", "rejected": "未通过", "pending": "待验收"}
        self.message = f"人工验收：{labels[state]}。请填写备注，然后选择“保存意见”。"
        self.ui.app.layout.focus(self.note)

    def cancel_review(self):
        if self.saving:
            self.message = "正在保存，请稍候"
            return
        self.reviewing = None
        self.note.text = ""
        self.message = "验收意见未提交"
        self.ui.app.layout.focus(self.menu)

    def save_review(self):
        if not self.reviewing or self.saving:
            return
        if not 1 <= len(self.note.text.strip()) <= 2000:
            self.message = "请填写 1–2000 字符的验收备注"
            return
        self.saving = True
        check_id, state = self.reviewing
        note = self.note.text
        self.message = "正在保存验收意见…"

        async def save():
            try:
                message = await asyncio.to_thread(
                    record_review,
                    self.store,
                    self.selector,
                    check_id,
                    state,
                    note,
                    expected_digest=self.report["revision"],
                )
            except Exception as error:
                self.message = f"验收未保存：{error}"
            else:
                self.reviewing = None
                self.note.text = ""
                self.message = message
                self.ui.app.layout.focus(self.menu)
            finally:
                self.saving = False
                if not self.reviewing:
                    self.refresh()
                self.ui.app.invalidate()

        self.schedule(save())

    def page_detail(self, direction):
        info = self.detail.window.render_info
        rows = max(1, info.window_height - 1) if info else 10
        (self.detail.buffer.cursor_down if direction > 0 else self.detail.buffer.cursor_up)(rows)
        self.ui.app.layout.focus(self.detail)

    def close(self):
        if self.reviewing:
            self.cancel_review()
            return
        self.generation += 1
        self.ui.report_page = None
        self.ui.app.layout.focus(self.ui.editor)
        self.ui.app.invalidate()


def report_bindings(ui, keys):
    active = Condition(lambda: ui.report_page is not None)
    keys.add("tab", filter=active)(focus_next)
    keys.add("s-tab", filter=active)(focus_previous)
    keys.add("escape", filter=active)(lambda _: ui.report_page.close())
    browsing = active & Condition(lambda: not ui.report_page.reviewing)
    keys.add("pageup", filter=browsing)(lambda _: ui.report_page.page_detail(-1))
    keys.add("pagedown", filter=browsing)(lambda _: ui.report_page.page_detail(1))
