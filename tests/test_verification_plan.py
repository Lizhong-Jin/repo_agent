"""Checks distinguish declared expectations, actual command receipts and user acceptance."""

import json

import pytest
from test_task_reports import begin, finish, record

from agent.report_reviews import record_review
from agent.task_reports import ReportStore, load_report
from agent.verification import PlanVerificationTool, normalize_cwd, plans_from_rows, validate_items
from cli.sessions_command import ui_command
from llm import ToolCall
from tools import ToolResult


def command_item(identity="unit"):
    return {
        "id": identity,
        "title": "validate saved content",
        "expectation": "assertions pass",
        "kind": "command",
        "command": ["python", "check.py"],
        "cwd": ".",
    }


def declare(c, capture, items, call_id="plan"):
    tool = PlanVerificationTool(
        lambda: plans_from_rows(c.ledger.evidence(run=capture.report["attempt_id"]))
    )
    call = ToolCall(call_id, "plan_verification", {"items": items})
    return record(c, capture, call, lambda: tool.execute(call.arguments))


def execute_check(c, capture, *, code=0, command=None, cwd=".", identity="run"):
    call = ToolCall(
        identity, "run_command", {"command": command or ["python", "check.py"], "cwd": cwd}
    )
    return record(c, capture, call, lambda: ToolResult(True, {"exit_code": code}))


def test_plan_and_manual_check_remain_unverified_until_execution(open_conversation):
    c = open_conversation()
    capture = begin(c)
    assert declare(
        c,
        capture,
        [
            command_item(),
            {
                "id": "windows",
                "title": "Windows UI",
                "expectation": "session switch works",
                "kind": "manual",
            },
        ],
    ).success
    items = finish(capture)["verification_items"]
    assert [i["state"] for i in items] == ["not_run", "manual_pending"]
    assert all(i["acceptance"]["state"] == "pending" for i in items)


def test_matching_check_passes_without_claiming_user_acceptance(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    execute_check(c, capture, cwd="./")
    item = finish(capture)["verification_items"][0]
    assert item["state"] == "passed"
    assert item["acceptance"]["state"] == "pending"
    assert item["attempts"][0]["ledger"]["result_ref"]
    assert "检查命令通过" in ui_command(c, "/report")


@pytest.mark.parametrize("variation", ["earlier", "different_argv", "different_cwd"])
def test_unrelated_or_retroactive_commands_do_not_count(open_conversation, variation):
    c = open_conversation()
    capture = begin(c)
    if variation == "earlier":
        execute_check(c, capture)
    declare(c, capture, [command_item()])
    if variation != "earlier":
        execute_check(
            c,
            capture,
            command=["python", "other.py"] if variation == "different_argv" else None,
            cwd="subdir" if variation == "different_cwd" else ".",
        )
    assert finish(capture)["verification_items"][0]["state"] == "not_run"


def test_later_failure_and_staleness_are_not_hidden(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    execute_check(c, capture, identity="first")
    execute_check(c, capture, code=1, identity="second")
    item = finish(capture)["verification_items"][0]
    assert item["state"] == "failed" and len(item["attempts"]) == 2


def test_modification_after_check_requires_revalidation(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    execute_check(c, capture)
    (c.store.project / "changed").write_text("later")
    assert finish(capture)["verification_items"][0]["state"] == "needs_recheck"


def test_ids_immutable_and_identical_declaration_idempotent(open_conversation):
    c = open_conversation()
    capture = begin(c)
    assert declare(c, capture, [command_item()]).success
    assert declare(c, capture, [command_item()], "repeat").data["registered"] == 0
    assert not declare(c, capture, [{**command_item(), "command": ["true"]}], "rewrite").success
    assert len(finish(capture)["verification_items"]) == 1


@pytest.mark.parametrize(
    "value",
    [
        {"state": "passed"},
        {"acceptance": "accepted"},
        {"command": "echo pass"},
        {"cwd": "../outside"},
        {"kind": "fake"},
        {"id": "bad/id"},
    ],
)
def test_model_cannot_supply_results_or_unsupported_fields(value):
    tool = PlanVerificationTool(lambda: [])
    result = tool.execute({"items": [{**command_item(), **value}]})
    assert not result.success and result.error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("cwd", ["/tmp", "C:\\temp", "../parent", "a/../../parent", "\\server"])
def test_working_directory_restrictions(cwd):
    with pytest.raises(ValueError):
        normalize_cwd(cwd)


def test_acceptance_is_append_only_and_does_not_change_machine_results(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    execute_check(c, capture, code=2)
    finish(capture)
    before = ReportStore(c.store).get()
    text = ui_command(c, '/report 1 --accept unit accepted --note "人工核实了具体需求"')
    assert "用户确认通过" in text and "检查失败" in text
    record_review(c.store, "1", "unit", "rejected", "复查发现遗漏")
    record_review(c.store, "1", "unit", "pending", "等待后续确认")
    assert ReportStore(c.store).get() == before
    item = load_report(c.store, c.ledger)["verification_items"][0]
    assert item["state"] == "failed" and item["acceptance"]["state"] == "pending"
    assert len(item["review_history"]) == 3


def test_review_does_not_apply_to_replaced_report(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    finish(capture)
    record_review(c.store, "latest", "unit", "accepted", "checked")
    changed = ReportStore(c.store).get()
    changed["outcome"]["notice"] = "new report revision"
    ReportStore(c.store).save(changed)
    item = load_report(c.store, c.ledger)["verification_items"][0]
    assert item["acceptance"]["state"] == "pending"
    assert item["review_history"]


def test_interrupted_plan_can_be_read_but_not_accepted(open_conversation):
    c = open_conversation()
    capture = begin(c)
    declare(c, capture, [command_item()])
    execute_check(c, capture)
    assert load_report(c.store, c.ledger)["verification_items"][0]["state"] == "unknown"
    with pytest.raises(ValueError, match="尚未完整"):
        record_review(c.store, "latest", "unit", "accepted", "checked")


def test_runtime_exposes_planner_and_it_has_no_acceptance_interface(open_conversation):
    c = open_conversation()
    definition = c.runtime.dispatcher.tools["plan_verification"].definition
    assert "acceptance" not in json.dumps(definition.parameters)
    tool = c.runtime.dispatcher.tools["plan_verification"]
    assert not tool.execute({"items": [command_item()]}).success


def test_plan_size_is_bounded():
    with pytest.raises(ValueError):
        validate_items([command_item(str(i)) for i in range(21)])
