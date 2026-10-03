"""Bridge runtime callbacks to a UI dispatcher without importing a UI framework.

Runtime callbacks run in the worker thread. Only immutable text/numeric snapshots
cross to the UI loop; runtime records and counters stay on the producer side.
"""


class SessionEvents:
    """Account and trace every event, with optional notices for line-mode output."""

    def __init__(self, status, tracer, *, show_notices=False, emit=print):
        self.status = status
        self.tracer = tracer
        self.show_notices = show_notices
        self.emit = emit

    def __call__(self, event, stats):
        self.status(event, stats)
        self.tracer(event, stats)
        if not self.show_notices:
            return
        if event.startswith("compaction_"):
            self.emit(f"[{stats.compaction['phase']}]", flush=True)
        elif event == "recovery":
            self.emit(f"[{stats.recoveries[-1]['message']}]", flush=True)
        elif event == "skill_loaded":
            self.emit(f"[已加载技能：{stats.skill_loads[-1]['name']}]", flush=True)


class RuntimeEventBridge:
    def __init__(
        self,
        runtime,
        *,
        dispatch,
        progress,
        model_boundary,
        thinking,
        status_text,
        write_meta,
        live,
        check_cancelled,
    ):
        self.runtime = runtime
        self.dispatch = dispatch
        self.progress = progress
        self.model_boundary = model_boundary
        self.thinking = thinking
        self.status_text = status_text
        self.write_meta = write_meta
        self.live = live
        self.check_cancelled = check_cancelled
        self.previous = None
        self.last_phase = None

    def __enter__(self):
        if self.previous is not None:
            raise RuntimeError("Runtime event bridge is already active")
        self.previous = (
            self.runtime.on_event,
            self.runtime.on_model_event,
            self.runtime.check_cancelled,
        )
        self.last_phase = None
        self.runtime.on_event = self.on_event
        # Replace the line-mode renderer to avoid displaying each delta twice.
        self.runtime.on_model_event = self.on_model_event
        self.runtime.check_cancelled = self.check_cancelled
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.runtime.on_event, self.runtime.on_model_event, self.runtime.check_cancelled = (
            self.previous
        )
        self.previous = None

    def on_event(self, name, stats):
        if self.previous[0]:
            self.previous[0](name, stats)
        # Preserve accounting/tracing order, then freeze the footer for the UI.
        footer = self.status_text()
        record = stats.model_calls[-1] if stats.model_calls else None
        phase = f"模型 #{record.step} · 等待响应…" if name == "model_start" else None
        if name == "tool_start":
            phase = f"工具 · {stats.current_tool.name} 执行中…"
        if name == "recovery":
            phase = stats.recoveries[-1]["message"]
            self.write_meta(f"[{phase}]")
        compact = record is not None and getattr(record, "purpose", "task") == "compaction"
        if compact and name == "model_start":
            phase = "正在生成历史摘要…"
        if name.startswith("compaction_"):
            phase = stats.compaction["phase"]
            self.write_meta(f"[{phase}]")
        if name == "skill_loaded":
            self.write_meta(f"[已加载技能：{stats.skill_loads[-1]['name']}]")
        self.dispatch(self.progress, phase, footer)
        if name in {"model_start", "model_end"} and not compact:
            self.dispatch(self.model_boundary, name, record.step)

    def on_model_event(self, kind, text, elapsed, record):
        phase = None
        if kind == "first_data" and record.first_text_seconds is None:
            phase = "已收到数据，等待正文…"
        elif kind == "thinking_start":
            phase = "正在思考…"
        elif kind == "thinking_end":
            phase = "思考片段已接收，等待后续输出…"
        elif kind in {"first_text", "text"}:
            phase = "正在输出…"
        if phase and self.last_phase != (id(record), phase):
            self.last_phase = (id(record), phase)
            self.dispatch(self.progress, f"模型 #{record.step} · {phase}", self.status_text())
        if kind in {"thinking_start", "thinking_delta", "thinking_end"}:
            self.dispatch(self.thinking, kind, text, record.step, elapsed)
            return None
        return self.live(kind, text, elapsed, record)
