"""Declared check plans; outcomes are derived exclusively from execution receipts."""

import posixpath
import re
from copy import deepcopy
from pathlib import PureWindowsPath

from llm import ToolDefinition
from tools._internal.base import ExecutionKind, ToolResult
from tools.scheduling import SERIAL

CHECK_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}")
MAX_CHECKS = 100


def normalize_cwd(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("cwd must be a workspace-relative directory")
    if PureWindowsPath(value).drive or value.startswith(("/", "\\")):
        raise ValueError("cwd must be workspace-relative")
    value = posixpath.normpath(value.replace("\\", "/"))
    if value == ".." or value.startswith("../"):
        raise ValueError("cwd must remain in the workspace")
    return value


def validate_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError("Provide 1-20 checks per call")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "title",
            "kind",
            "command",
            "cwd",
            "expectation",
        }:
            raise ValueError("Unsupported check field; results and acceptance cannot be supplied")
        identity, title = item.get("id"), item.get("title")
        expectation, kind = item.get("expectation"), item.get("kind")
        if (
            not isinstance(identity, str)
            or not CHECK_ID.fullmatch(identity)
            or identity in seen
            or not isinstance(title, str)
            or not 1 <= len(title.strip()) <= 200
            or not isinstance(expectation, str)
            or not 1 <= len(expectation.strip()) <= 2000
            or not isinstance(kind, str)
            or kind not in {"command", "manual"}
        ):
            raise ValueError("Checks require unique IDs, a title, kind and an expectation")
        record = {
            "id": identity,
            "title": title.strip(),
            "kind": kind,
            "expectation": expectation.strip(),
        }
        if kind == "command":
            command = item.get("command")
            if (
                not isinstance(command, list)
                or not 1 <= len(command) <= 128
                or any(
                    not isinstance(arg, str) or "\0" in arg or len(arg) > 8192 for arg in command
                )
                or not command[0]
                or sum(map(len, command)) > 32768
            ):
                raise ValueError("Command checks require a bounded argv array")
            record.update(command=list(command), cwd=normalize_cwd(item.get("cwd", ".")))
        elif "command" in item or "cwd" in item:
            raise ValueError("Manual checks cannot declare a command")
        seen.add(identity)
        result.append(record)
    return result


def plans_from_rows(rows):
    plans = []
    for row in sorted(rows, key=lambda r: r["seq"]):
        if row["name"] != "plan_verification" or row["state"] != "returned":
            continue
        for item in (row["effects"] or {}).get("result", {}).get("verification_plan", []):
            plans.append({**item, "declared_event": row["seq"], "call_id": row["call_id"]})
    return plans


class PlanVerificationTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    scheduling_policy = SERIAL

    def __init__(self, existing):
        self.existing = existing

    @property
    def definition(self):
        return ToolDefinition(
            "plan_verification",
            "Declare checks BEFORE running them. Each command check matches later run_command "
            "calls in this task using the exact argv and normalized cwd. An exit code is evidence "
            "about the check command, not proof of requirements completion. Use assertions that "
            "exit nonzero on failure. Manual checks remain unverified until the user reviews them. "
            "This tool cannot set outcomes or user acceptance. IDs are immutable within a task; "
            "repeat identical declarations safely, and use a new ID for a revised check.",
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "pattern": r"^[a-zA-Z0-9_-]{1,64}$"},
                                "title": {"type": "string", "maxLength": 200},
                                "kind": {"type": "string", "enum": ["command", "manual"]},
                                "expectation": {"type": "string", "maxLength": 2000},
                                "command": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 128,
                                },
                                "cwd": {"type": "string", "default": "."},
                            },
                            "required": ["id", "title", "kind", "expectation"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments):
        try:
            if not isinstance(arguments, dict) or set(arguments) != {"items"}:
                raise ValueError("Allowed field: items")
            items = validate_items(arguments["items"])
            existing = {
                p["id"]: {k: v for k, v in p.items() if k not in {"declared_event", "call_id"}}
                for p in self.existing()
            }
            fresh = []
            for item in items:
                if item["id"] in existing:
                    if existing[item["id"]] != item:
                        raise ValueError("An existing check cannot be redefined; use a new ID")
                else:
                    fresh.append(item)
            if len(existing) + len(fresh) > MAX_CHECKS:
                raise ValueError("Task verification plan limit reached")
        except ValueError as error:
            return ToolResult(False, error_code="INVALID_ARGUMENTS", error=str(error))
        return ToolResult(True, {"items": items, "registered": len(fresh)}).with_effects(
            "none", details={"verification_plan": fresh}
        )


def verification_items(report):
    items = []
    for plan in report.get("verification_plan", []):
        matches = []
        if plan["kind"] == "command":
            for run in report.get("executions", []):
                if run["tool"] != "run_command" or run["ledger"]["event"] <= plan["declared_event"]:
                    continue
                try:
                    cwd = normalize_cwd(run["arguments"].get("cwd", "."))
                except ValueError:
                    continue
                if run["arguments"].get("command") == plan["command"] and cwd == plan["cwd"]:
                    matches.append(
                        {
                            key: deepcopy(run[key])
                            for key in ("state", "freshness", "exit_code", "ledger")
                        }
                    )
        state = "not_run" if plan["kind"] == "command" else "manual_pending"
        if matches:
            last = matches[-1]
            if last["state"] == "command_succeeded":
                state = {"unchanged": "passed", "needs_recheck": "needs_recheck"}.get(
                    last["freshness"], "unknown"
                )
            else:
                state = {"not_executed": "not_run"}.get(last["state"], last["state"])
        items.append(
            {
                **deepcopy(plan),
                "state": state,
                "attempts": matches,
                "acceptance": {"state": "pending"},
            }
        )
    return items


def render_verification(items):
    if not items:
        return "验证清单：未登记检查项目；不代表已完成验收。"
    labels = {
        "passed": "检查命令通过",
        "failed": "检查失败",
        "timed_out": "检查超时",
        "not_run": "尚未执行",
        "manual_pending": "待人工检查",
        "unknown": "结果未确认",
        "needs_recheck": "文件变化，需重新验证",
    }
    acceptance = {"pending": "待验收", "accepted": "用户确认通过", "rejected": "用户确认未通过"}
    lines = ["验证清单（命令结果与用户验收分别记录）："]
    for item in items:
        review = item["acceptance"]
        lines.append(
            f"  [{item['id']}] {item['title']} · {labels[item['state']]} · "
            f"{acceptance[review['state']]}"
        )
        lines.append(f"    预期：{item['expectation']}")
        if item["kind"] == "command":
            lines.append(f"    命令：{item['command']} · 目录：{item['cwd']}")
        if item["attempts"]:
            lines.append(
                f"    执行 {len(item['attempts'])} 次；最近证据："
                f"{item['attempts'][-1]['ledger']['result_ref']}"
            )
        if review.get("note"):
            lines.append(f"    验收备注：{review['note']}（{review['at']}）")
    return "\n".join(lines)
