"""Execution spans, usage accounting and file-only tracing for the Agent."""

import json
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from llm import Message, ToolCall, Usage


@dataclass
class ModelCallRecord:
    step: int
    first_data_seconds: float | None = None
    first_text_seconds: float | None = None
    first_display_seconds: float | None = None
    response_seconds: float | None = None
    thinking: dict | None = None
    status: str = "running"
    elapsed_seconds: float = 0.0
    usage: Usage | None = None
    finish_reason: str | None = None
    error_type: str | None = None


@dataclass
class ToolCallRecord:
    step: int
    call_id: str
    name: str
    arguments: dict[str, Any]
    status: str = "running"
    elapsed_seconds: float = 0.0
    error_code: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    cleanup_failed: bool = False


@dataclass
class RunStats:
    task_number: int
    status: str = "running"
    elapsed_seconds: float = 0.0
    model_calls: list[ModelCallRecord] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    skill_loads: list[dict[str, str]] = field(default_factory=list)
    error_type: str | None = None

    task_id: str = field(default_factory=lambda: uuid4().hex)
    trace_error: str | None = None

    def token_total(self, field_name: str) -> tuple[int | None, int]:
        """Return the known sum and the number of calls with a reported counter."""
        values = []
        for call in self.model_calls:
            if call.usage is None:
                continue
            value = getattr(call.usage, field_name)
            if field_name == "total_tokens" and value is None:
                if call.usage.input_tokens is not None and call.usage.output_tokens is not None:
                    value = call.usage.input_tokens + call.usage.output_tokens
            if value is not None:
                values.append(value)
        return (sum(values) if values else None, len(values))


def summarize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Record file targets and small options without copying source text or secrets."""
    visible = {
        "path",
        "start_line",
        "end_line",
        "overwrite",
        "create_parents",
        "parents",
        "include_hidden",
        "case_sensitive",
        "glob",
    }
    summary = {}
    for key, value in arguments.items():
        if key in visible and isinstance(value, (str, bool, int, float)):
            summary[key] = value[:300] if isinstance(value, str) else value
        elif isinstance(value, str):
            summary[key] = f"<省略正文，{len(value)} 字符>"
        else:
            summary[key] = "<省略>"
    return summary


def _tokens(stats: RunStats, name: str) -> str:
    total, reported = stats.token_total(name)
    if total is None:
        return "未返回"
    if reported < len(stats.model_calls):
        return f"{total}（仅 {reported}/{len(stats.model_calls)} 次已知）"
    return str(total)


# Translate structured json data into a form that is easy for humans to understand
def format_event(event: str, stats: RunStats) -> str:
    prefix = (
        f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] [任务 {stats.task_number}]"
    )
    if event == "task_start":
        text = f"开始执行: task_id={stats.task_id}"
    elif event == "skill_loaded":
        skill = stats.skill_loads[-1]
        text = f"已加载技能：{skill['name']}，来源={skill['source']}，方式={skill['invocation']}"
    elif event.startswith("model_"):
        call = stats.model_calls[-1]
        if event == "model_start":
            text = f"模型调用 #{call.step} 开始"
        else:
            usage = call.usage
            counters = (
                f"输入={usage.input_tokens if usage.input_tokens is not None else '未返回'}, "
                f"输出={usage.output_tokens if usage.output_tokens is not None else '未返回'}"
                if usage
                else "用量未返回"
            )
            text = (
                f"模型调用 #{call.step} {call.status}，耗时 {call.elapsed_seconds:.3f}s, "
                f"tokens: {counters}，结束原因: {call.finish_reason or call.error_type or '未知'}"
                f"，首行数据={call.first_data_seconds}，首字={call.first_text_seconds}"
                f"，首次显示={call.first_display_seconds}，响应总时长={call.response_seconds}"
                f"，思考设置={json.dumps(call.thinking, ensure_ascii=False)}"
            )
    elif event.startswith("tool_"):
        call = stats.tool_calls[-1]
        label = f"工具 {call.name}（模型轮次 {call.step}, id={json.dumps(call.call_id)}, "
        if event == "tool_start":
            text = f"{label} 开始, 参数={json.dumps(call.arguments, ensure_ascii=False)}"
        else:
            text = f"{label} {call.status}，耗时 {call.elapsed_seconds:.3f}s"
            if call.error_code:
                text += f"，错误码={call.error_code}"
            if call.exit_code is not None:
                text += f"，命令退出码={call.exit_code}"
            if call.timed_out:
                text += "，命令超时"
            if call.cleanup_failed:
                text += "，进程清理未确认"
    else:
        failures = sum(call.status != "success" for call in stats.tool_calls)
        text = (
            f"统计：状态={stats.status}，模型调用={len(stats.model_calls)} 次，"
            f"工具调用={len(stats.tool_calls)} 次（失败/中断 {failures} 次），"
            f"总耗时={stats.elapsed_seconds:.3f}s\n"
            f"{prefix} tokens: 输入={_tokens(stats, 'input_tokens')}, "
            f"输出={_tokens(stats, 'output_tokens')}，合计={_tokens(stats, 'total_tokens')}; "
            f"缓存读取={_tokens(stats, 'cached_input_tokens')}, "
            f"缓存写入={_tokens(stats, 'cache_write_tokens')}, "
            f"思考={_tokens(stats, 'reasoning_tokens')}"
        )
        if stats.error_type:
            text += f"，异常类型={stats.error_type}"
    if event == "task_start":
        record = f"\n{prefix} {text}"
    else:
        record = f"{prefix} {text}"
    return record


def _failure_status(error: BaseException) -> str:
    return "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"


class RunTrace:
    """Record execution without making the runtime responsible for timers or log formatting."""

    def __init__(self, task_number: int, on_event: Callable[[str, RunStats], None] | None):
        self.stats = RunStats(task_number)
        self.on_event = on_event

    def emit(self, event: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, deepcopy(self.stats))
            except Exception as error:
                # Never replay completed file operations because their logger failed.
                self.stats.trace_error = type(error).__name__

    def skill_loaded(self, skill, invocation: str) -> None:
        record = {
            "name": skill.name,
            "source": skill.source,
            "sha256": skill.sha256,
            "invocation": invocation,
        }
        if record not in self.stats.skill_loads:
            self.stats.skill_loads.append(record)
            self.emit("skill_loaded")

    def __enter__(self):
        self.started = perf_counter()
        self.emit("task_start")
        return self

    def __exit__(self, exc_type, error, traceback):
        if error is not None:
            self.stats.status = _failure_status(error)
            self.stats.error_type = type(error).__name__
        self.stats.elapsed_seconds = perf_counter() - self.started
        self.emit("task_end")

    @contextmanager
    def model(self, step: int):
        record = ModelCallRecord(step)
        self.stats.model_calls.append(record)
        started = perf_counter()
        self.emit("model_start")
        try:
            yield record
            record.status = "success"
        except BaseException as error:
            record.status = _failure_status(error)
            record.error_type = type(error).__name__
            raise
        finally:
            record.elapsed_seconds = perf_counter() - started
            self.emit("model_end")

    @contextmanager
    def tool(self, step: int, call: ToolCall):
        record = ToolCallRecord(step, call.id, call.name, summarize_arguments(call.arguments))
        self.stats.tool_calls.append(record)
        started = perf_counter()
        self.emit("tool_start")
        try:
            yield record
        except BaseException as error:
            record.status = _failure_status(error)
            record.error_code = type(error).__name__
            raise
        finally:
            record.elapsed_seconds = perf_counter() - started
            self.emit("tool_end")

    @staticmethod
    def tool_result(record: ToolCallRecord, observation: Message) -> None:
        record.status = "failed" if observation.is_error else "success"
        payload = json.loads(observation.content)
        if observation.is_error:
            record.error_code = payload["error"]["code"]
        data = payload.get("data", {})
        if type(data.get("exit_code")) is int:
            record.exit_code = data["exit_code"]
        record.timed_out = bool(data.get("timed_out"))
        record.cleanup_failed = bool(data.get("cleanup_error"))


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)


def _usage_summary(stats: RunStats) -> dict:
    summary = {}
    for name in TOKEN_FIELDS:
        total, reported = stats.token_total(name)
        summary[name] = {
            "known_total": total,
            "reported_calls": reported,
            "total_calls": len(stats.model_calls),
            "complete": reported == len(stats.model_calls),
        }
    return summary


class Tracer:
    """Write readable and JSONL traces; never print routine events to the terminal.

    Use as a context manager and pass the instance as AgentRuntime.on_event.
    Files are exclusive, permission 600, and flushed after every event.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        session_id: str | None = None,
        provider: str = "",
        model: str = "",
        workspace: str = "",
    ):
        self.session_id = session_id or f"session_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", self.session_id):
            raise ValueError("Invalid trace session ID")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.text_path = directory / f"{self.session_id}.trace.log"
        self.jsonl_path = directory / f"{self.session_id}.trace.jsonl"
        self.metadata = {"provider": provider, "model": model, "workspace": workspace}
        self.error: str | None = None
        self._files = []
        self._tasks = []
        self._started = perf_counter()

    def __enter__(self):
        try:
            for path in (self.text_path, self.jsonl_path):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._files.append(os.fdopen(fd, "w", encoding="utf-8"))
            self._write(
                {"event": "session_start", **self.metadata},
                f"会话开始: {self.session_id}, {json.dumps(self.metadata, ensure_ascii=False)}",
            )
        except BaseException:
            for output in self._files:
                output.close()
            raise
        return self

    def _write(self, data: dict, text: str) -> None:
        if self.error is not None:
            return
        record = {
            "schema_version": 1,
            "timestamp": datetime.now().astimezone().isoformat(),
            "session_id": self.session_id,
            **data,
        }
        try:
            self._files[0].write(text + "\n")
            self._files[0].flush()
            self._files[1].write(json.dumps(record, ensure_ascii=False) + "\n")
            self._files[1].flush()
        except OSError as error:
            self.error = type(error).__name__

    def __call__(self, event: str, stats: RunStats) -> None:
        record = {"event": event, "task_id": stats.task_id, "task_number": stats.task_number}
        if event.startswith("model_"):
            record["model_call"] = asdict(stats.model_calls[-1])
        elif event == "skill_loaded":
            record["skill"] = stats.skill_loads[-1]
        elif event.startswith("tool_"):
            record["tool_call"] = asdict(stats.tool_calls[-1])
        elif event == "task_end":
            record.update(
                status=stats.status,
                elapsed_seconds=stats.elapsed_seconds,
                model_calls=len(stats.model_calls),
                tool_calls=len(stats.tool_calls),
                tool_failures=sum(c.status != "success" for c in stats.tool_calls),
                skill_loads=stats.skill_loads,
                error_type=stats.error_type,
                usage=_usage_summary(stats),
            )
            self._tasks.append(record)
        self._write(record, format_event(event, stats))

    def __exit__(self, exc_type, error, traceback):
        usage = {}
        for name in TOKEN_FIELDS:
            parts = [task["usage"][name] for task in self._tasks]
            known = [part["known_total"] for part in parts if part["known_total"] is not None]
            usage[name] = {
                "known_total": sum(known) if known else None,
                "reported_calls": sum(part["reported_calls"] for part in parts),
                "total_calls": sum(part["total_calls"] for part in parts),
                "complete": all(part["complete"] for part in parts),
            }
        counts = {
            status: sum(task["status"] == status for task in self._tasks)
            for status in ("completed", "max_steps", "stopped", "failed", "interrupted")
        }
        summary = {
            "event": "session_end",
            "tasks": len(self._tasks),
            "task_statuses": counts,
            "elapsed_seconds": perf_counter() - self._started,
            "task_elapsed_seconds": sum(task["elapsed_seconds"] for task in self._tasks),
            "model_calls": sum(task["model_calls"] for task in self._tasks),
            "tool_calls": sum(task["tool_calls"] for task in self._tasks),
            "usage": usage,
            "error_type": type(error).__name__ if error is not None else None,
        }
        total = usage["total_tokens"]
        known = total["known_total"] if total["known_total"] is not None else "未返回"
        self._write(
            summary,
            f"会话结束：任务 {len(self._tasks)} 个，"
            f"状态分布={json.dumps(counts, ensure_ascii=False)}\n"
            f"会话统计：模型调用={summary['model_calls']} 次，工具调用={summary['tool_calls']} 次，"
            f"任务执行总耗时={summary['task_elapsed_seconds']:.3f}s, "
            f"会话总耗时（含输入等待）={summary['elapsed_seconds']:.3f}s, "
            f"tokens 合计={known} ({total['reported_calls']}/{total['total_calls']} 次已知)",
        )
        for output in self._files:
            try:
                output.close()
            except OSError as close_error:
                self.error = type(close_error).__name__
