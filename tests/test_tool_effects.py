"""Effects belong to implementations, survive transport, and stay out of model output."""

import json
from dataclasses import asdict

import pytest

from host_support.cancellation import cancellation_scope
from host_support.execution_receipt import receipt_scope
from llm import ToolCall, ToolDefinition
from sandbox.worker import execute_request
from tools import ExecutionKind, ReadFileTool, ToolDispatcher, ToolEffects, ToolResult


@pytest.mark.parametrize("name", ["new_custom_writer", "write_file"])
def test_dispatcher_records_explicit_effects_independent_of_name(name):
    class Custom:
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = ToolDefinition(name, "test")

        def execute(self, arguments):
            return ToolResult(True, {"output": "not a receipt"}).with_effects(
                details={"custom_change": {"resource": "x", "revision": 2}}
            )

    dispatcher = ToolDispatcher()
    dispatcher.register(Custom())
    receipts = []
    with cancellation_scope() as context, receipt_scope(lambda *args: receipts.append(args)):
        result = dispatcher.execute(name, {}, call_id="1")
    record = context.report()["changes"][0]
    assert record["effects"] == "reported" and record["call_id"] == "1"
    assert record["result"] == {"custom_change": {"resource": "x", "revision": 2}}
    assert receipts[0] == (result, record)


def test_old_tool_name_and_data_do_not_imply_reported_effects():
    class Unknown:
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = ToolDefinition("write_file", "test")

        def execute(self, arguments):
            return ToolResult(False, {"path": "x", "created": True})

    dispatcher = ToolDispatcher()
    dispatcher.register(Unknown())
    with cancellation_scope() as context:
        dispatcher.execute("write_file", {})
    report = context.report()
    assert report["changes"] == []
    assert report["tools"][0]["effects"] == "unknown"
    assert report["tools"][0]["result"] == {}


def test_effect_snapshot_worker_roundtrip_and_model_exclusion():
    data = {"changes": [{"path": "a"}]}
    result = ToolResult(True, data).with_effects()
    data["changes"][0]["path"] = "b"
    assert result.effects.details["changes"][0]["path"] == "a"
    restored = ToolResult(**json.loads(json.dumps(asdict(result))))
    assert restored == result
    record = restored.effects.to_record()
    record["result"]["changes"].clear()
    assert restored.effects.details["changes"]
    message = json.loads(restored.to_message(ToolCall("1", "custom", {})).content)
    assert message == {"success": True, "data": data}
    assert ToolResult(**{"success": True, "data": {}}).effects.status == "unknown"


@pytest.mark.parametrize("effects", [{"status": "complete"}, {"details": []}, "reported"])
def test_invalid_worker_effects_rejected(effects):
    with pytest.raises((TypeError, ValueError)):
        ToolResult(True, effects=effects)


def test_file_worker_transmits_effects_and_reads_report_none(tmp_path, capsys):
    execute_request({"name": "write_file", "arguments": {"path": "a", "content": "x"}}, tmp_path)
    result = ToolResult(**json.loads(capsys.readouterr().out))
    assert result.effects.status == "reported"
    assert result.effects.details["path"] == "a"
    assert result.effects.details["sha256"] == result.data["sha256"]
    reader = ReadFileTool(tmp_path)
    assert reader.execute({"reads": [{"path": "a"}]}).effects == ToolEffects("none")
    assert reader.execute({}).effects == ToolEffects("none")


def test_process_diagnostics_never_claim_exhaustive_changes():
    effects = ToolEffects.process(
        {"exit_code": 0, "cleanup_status": "confirmed", "stdout": "private", "path": "x"}
    )
    assert effects.status == "unknown"
    assert effects.details == {"exit_code": 0, "cleanup_status": "confirmed"}
