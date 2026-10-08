"""Persist readable durations without changing calculations or raw evidence."""

import json
from copy import deepcopy

from test_task_reports import begin

from agent.task_reports import ReportStore
from agent.Tracing import ModelCallRecord, RunStats, ToolCallRecord, Tracer, format_event
from agent.transcript import Transcript
from llm import Usage


def test_readable_trace_formats_all_model_latencies_and_missing_values():
    call = ModelCallRecord(
        1,
        elapsed_seconds=1.23456789,
        first_data_seconds=0.12345678,
        first_text_seconds=0.23456789,
        first_thinking_seconds=None,
        first_display_seconds=0.34567891,
        response_seconds=1.12345678,
    )
    stats = RunStats(1, model_calls=[call])
    text = format_event("model_end", stats)
    for value in (
        "耗时 1.235s",
        "首行数据=0.123s",
        "首字=0.235s",
        "首段思考=未返回",
        "首次显示=0.346s",
        "响应总时长=1.123s",
    ):
        assert value in text
    assert call.elapsed_seconds == 1.23456789
    assert call.first_text_seconds == 0.23456789


def test_jsonl_rounds_owned_timings_only_in_both_trace_destinations(open_conversation):
    c = open_conversation()
    precise = 0.123456789123
    call = ModelCallRecord(
        1,
        elapsed_seconds=precise,
        first_data_seconds=precise,
        first_text_seconds=precise,
        first_thinking_seconds=None,
        first_display_seconds=precise,
        response_seconds=precise,
        thinking={"budget_seconds": precise},
        usage=Usage(123, 456),
    )
    tool = ToolCallRecord(
        1, "call", "example", {"duration_seconds": precise}, elapsed_seconds=precise
    )
    stats = RunStats(1, model_calls=[call], tool_calls=[tool], elapsed_seconds=precise)
    original = deepcopy(stats)
    with Tracer(c.store.project / "logs", session_store=c.store) as writer:
        writer("model_end", stats)
        writer("tool_end", stats)
        writer("task_end", stats)
    assert stats == original
    persistent = c.store.catalog.log_path(c.store.id, "jsonl")
    assert writer.jsonl_path.read_text() == persistent.read_text()
    events = [json.loads(line) for line in persistent.read_text().splitlines()]
    model = next(e["model_call"] for e in events if e["event"] == "model_end")
    for key, value in model.items():
        if key.endswith("_seconds"):
            assert value is None or value == 0.123
    assert model["thinking"]["budget_seconds"] == precise
    assert model["usage"]["input_tokens"] == 123
    result = next(e["tool_call"] for e in events if e["event"] == "tool_end")
    assert result["elapsed_seconds"] == 0.123
    assert result["arguments"]["duration_seconds"] == precise
    assert events[-2]["elapsed_seconds"] == events[-1]["task_elapsed_seconds"] == 0.123
    assert events[-1]["elapsed_seconds"] == round(events[-1]["elapsed_seconds"], 3)


def test_task_totals_are_rounded_after_aggregation(tmp_path):
    with Tracer(tmp_path) as writer:
        for number in (1, 2):
            writer("task_end", RunStats(number, elapsed_seconds=0.00049))
        assert writer._tasks[0]["elapsed_seconds"] == 0.00049
    events = [json.loads(line) for line in writer.jsonl_path.read_text().splitlines()]
    assert [e["elapsed_seconds"] for e in events if e["event"] == "task_end"] == [0.0, 0.0]
    assert events[-1]["task_elapsed_seconds"] == 0.001


def test_transcript_rounds_on_save_but_keeps_live_timers_and_old_records():
    transcript = Transcript()
    transcript.thinking("thinking_start", "", 1, 0.123456789)
    transcript.thinking("thinking_end", "", 1, 0.234567891)
    transcript.thinking("thinking_start", "", 2, 0.345678912)
    records = transcript.to_records()
    assert records[0]["started"] == 0.123 and records[0]["ended"] == 0.235
    assert records[1]["started"] == 0.346 and records[1]["ended"] is None
    assert transcript.blocks[0].started == 0.123456789
    assert transcript.blocks[0].ended == 0.234567891
    restored = Transcript.from_records(records)
    assert restored.to_records() == records
    records[0].update(started=0.123456789, ended=0.234567891)
    assert Transcript.from_records(records).blocks[0].started == 0.123456789


def test_report_rounds_only_metrics_and_does_not_mutate_capture(open_conversation):
    c = open_conversation()
    capture = begin(c)
    precise = 0.123456789123
    report = capture.report
    report["baseline"]["metrics"]["scan_seconds"] = precise
    report["metrics"] = {
        "start_seconds": precise,
        "scans": [{"scan_seconds": precise, "read_bytes": 12345, "complete": True}],
    }
    report["executions"] = [
        {"arguments": {"timeout_seconds": precise}, "process": {"value": precise}}
    ]
    report["host_verification"] = {"data": {"duration_seconds": precise}}
    original = deepcopy(report)
    ReportStore(c.store).save(report)
    saved = ReportStore(c.store).get()
    assert report == original
    assert saved["metrics"]["start_seconds"] == 0.123
    assert saved["metrics"]["scans"][0] == {
        "scan_seconds": 0.123,
        "read_bytes": 12345,
        "complete": True,
    }
    assert saved["baseline"]["metrics"]["scan_seconds"] == 0.123
    assert saved["executions"] == report["executions"]
    assert saved["host_verification"] == report["host_verification"]
    assert saved["baseline"]["files"] == report["baseline"]["files"]
