"""Task delivery reports derived from snapshots and durable execution receipts.

No model-generated text can establish file attribution or successful validation.
Historical reports are read without rescanning the current workspace.
"""

import hashlib
import json
import os
import re
import stat
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from host_support.change_evidence import content_record, difference
from host_support.filesystem import open_file, stat_at, walk_descriptors
from host_support.storage import atomic_write, sync_directory
from tools._internal.file_policy import is_protected_name, runtime_protected_paths

from .record_timing import metric_seconds
from .verification import plans_from_rows, render_verification, verification_items

SKIP_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "target",
    }
)
PROCESS_TOOLS = frozenset({"run_command", "run_python", "run_shell"})
MAX_FILES = 10000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_SCAN_SECONDS = 3
MAX_REPORT_BYTES = 48 * 1024 * 1024


def now():
    return datetime.now(UTC).isoformat()


def _raise_scan_error(error):
    raise error


def snapshot(root, *, exclude=(), texts=True):
    root = Path(root)
    result = {
        "root": str(root),
        "at": now(),
        "files": {},
        "complete": True,
        "issues": [],
        "excluded": [],
        "policy": sorted(SKIP_DIRS),
    }
    files = result["files"]
    started, read_bytes, text_bytes = time.monotonic(), 0, 0
    excluded = [Path(p).absolute() for p in (*exclude, *runtime_protected_paths(root))]
    try:
        walker = (
            os.fwalk(root, follow_symlinks=False, onerror=_raise_scan_error)
            if os.name == "posix"
            else walk_descriptors(root)
        )
        for directory, dirs, names, fd in walker:
            if time.monotonic() - started > MAX_SCAN_SECONDS:
                raise OSError("snapshot budget exceeded")
            relative = Path(directory).relative_to(root)
            for name in list(dirs):
                path = Path(directory) / name
                if (
                    name in SKIP_DIRS
                    or is_protected_name(relative / name)
                    or any(path.is_relative_to(p) for p in excluded)
                ):
                    dirs.remove(name)
                    result["excluded"].append((relative / name).as_posix())
                elif stat.S_ISLNK(stat_at(name, dir_fd=fd).st_mode):
                    dirs.remove(name)
                    result["excluded"].append((relative / name).as_posix())
            for name in names:
                path = relative / name
                absolute = Path(directory) / name
                if is_protected_name(path) or any(absolute.is_relative_to(p) for p in excluded):
                    result["excluded"].append(path.as_posix())
                    continue
                if (
                    len(files) >= MAX_FILES
                    or read_bytes >= MAX_SCAN_BYTES
                    or time.monotonic() - started > MAX_SCAN_SECONDS
                ):
                    raise OSError("snapshot budget exceeded")
                key = path.as_posix()
                try:
                    descriptor = open_file(name, dir_fd=fd)
                    with os.fdopen(descriptor, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                            raise ValueError("not an independent regular file")
                        if before.st_size > MAX_FILE_BYTES:
                            raise ValueError("file exceeds snapshot size limit")
                        raw = stream.read(min(MAX_FILE_BYTES, MAX_SCAN_BYTES - read_bytes) + 1)
                        after = os.fstat(stream.fileno())
                    read_bytes += len(raw)
                    if (
                        len(raw) != before.st_size
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or before.st_ctime_ns != after.st_ctime_ns
                    ):
                        raise ValueError("file changed during snapshot")
                    record = content_record(raw)
                    record["mode"] = stat.S_IMODE(after.st_mode)
                    if not texts or text_bytes + len(raw) > MAX_TEXT_BYTES:
                        record.pop("text", None)
                    if "text" in record:
                        text_bytes += len(raw)
                    files[key] = record
                except (OSError, ValueError) as error:
                    files[key] = {"exists": True, "unknown": type(error).__name__}
                    result["complete"] = False
                    result["issues"].append(f"{key}: {error}")
    except (OSError, ValueError) as error:
        result["complete"] = False
        result["issues"].append(str(error))
    result["metrics"] = {
        "scan_seconds": time.monotonic() - started,
        "read_bytes": read_bytes,
        "files": len(files),
        "retained_text_bytes": text_bytes,
    }
    return result


def fingerprint(snap):
    values = {p: {k: v for k, v in f.items() if k != "text"} for p, f in snap["files"].items()}
    return {
        "hash": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
        "complete": snap["complete"],
        "at": snap["at"],
    }


def file_at(snap, path):
    if path in snap["files"]:
        return snap["files"][path]
    excluded = any(Path(path).is_relative_to(p) for p in snap.get("excluded", []))
    return {"exists": False} if snap["complete"] and not excluded else {"unknown": True}


def changes(before, after):
    result = []
    for path in sorted(before["files"].keys() | after["files"].keys()):
        a, b = file_at(before, path), file_at(after, path)
        if {k: v for k, v in a.items() if k != "text"} == {
            k: v for k, v in b.items() if k != "text"
        }:
            continue
        kind = (
            "unknown"
            if a.get("unknown") or b.get("unknown")
            else "added"
            if a.get("exists") is False
            else "deleted"
            if b.get("exists") is False
            else "modified"
        )
        result.append(
            {
                "path": path,
                "kind": kind,
                "attribution": "unconfirmed",
                "before": {k: v for k, v in a.items() if k != "text"},
                "after": {k: v for k, v in b.items() if k != "text"},
                **difference(a, b, path),
            }
        )
    return result


class ReportStore:
    def __init__(self, store):
        self.store = store
        self.directory = store.directory / "reports" / store.id

    def save(self, report):
        for directory in (self.directory.parent, self.directory):
            if directory.is_symlink():
                raise ValueError("报告目录不能是符号链接")
            directory.mkdir(mode=0o700, exist_ok=True)
        identity = report["attempt_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("无效的报告标识")
        # Round our diagnostics only. Raw tool receipts, arguments, file metadata and
        # hashes must keep their exact values; the in-memory capture is not mutated.
        exported = dict(report)
        for key in ("baseline", "final", "host_baseline", "host_final"):
            if key in report and "metrics" in report[key]:
                exported[key] = {**report[key], "metrics": metric_seconds(report[key]["metrics"])}
        if "metrics" in report:
            exported["metrics"] = metric_seconds(report["metrics"])
        payload = json.dumps(exported, ensure_ascii=False).encode()
        if len(payload) > MAX_REPORT_BYTES:
            raise ValueError("报告超过大小上限")
        path = self.directory / f"{identity}.json"
        atomic_write(path, payload, mode=0o600, sync=True)
        sync_directory(self.directory)
        return path

    def get(self, selector="latest"):
        if self.directory.is_symlink() or self.directory.parent.is_symlink():
            raise ValueError("报告目录不能是符号链接")
        selected = None
        for path in self.directory.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
                continue  # Acceptance sidecars are separate from machine reports.
            fd = open_file(path)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_size > MAX_REPORT_BYTES
                ):
                    raise ValueError("无效的报告文件")
                report = json.loads(stream.read(MAX_REPORT_BYTES + 1))
            if report.get("version") != 1 or report.get("session_id") != self.store.id:
                raise ValueError("报告版本或会话不匹配")
            if selector == "latest" or str(report["task_number"]) == str(selector):
                if selected is None or report["task_number"] > selected["task_number"]:
                    selected = report
        if selected is None:
            raise ValueError("没有对应任务报告；旧任务不会用当前文件补造历史快照")
        return selected


def ledger_rows(ledger, attempt):
    rows, before = [], None
    while True:
        batch = ledger.evidence(limit=500, before=before, run=attempt)
        rows.extend(row for row in batch if row["run"] == attempt)
        if len(batch) < 500:
            break
        before = batch[-1]["seq"]
    return sorted(rows, key=lambda row: row["seq"])


def evidence(report, ledger, final=None, *, checks=None):
    operations, executions, failures = [], [], []
    rows = ledger_rows(ledger, report["attempt_id"])
    for row in rows:
        ref = {
            "run": row["run"],
            "call_id": row["call_id"],
            "event": row["seq"],
            "result_ref": ledger.result_reference(row["seq"])
            if row["state"] == "returned"
            else None,
        }
        effects = row["effects"] or {}
        payload = json.loads(row["observation"]["content"]) if row["observation"] else {}
        if row["state"] in {"started", "uncertain"} or payload.get("success") is False:
            failures.append(
                {
                    "tool": row["name"],
                    "state": row["state"],
                    "error": payload.get("error"),
                    "ledger": ref,
                }
            )
        # Only internal effects metadata is evidence. Never read claims from stdout/model text.
        for op in effects.get("result", {}).get("file_changes", []):
            operation = {**op, "tool": row["name"], "ledger": ref, "final_state": "unknown"}
            if final is not None:
                current = file_at(final, op["path"])
                expected = op["after"]
                if expected.get("exists") is False and current.get("exists") is False:
                    operation["final_state"] = "matches"
                elif expected.get("sha256") and current.get("sha256"):
                    operation["final_state"] = (
                        "matches" if expected["sha256"] == current["sha256"] else "changed_after"
                    )
                elif expected.get("sha256") and current.get("exists") is False:
                    operation["final_state"] = "changed_after"
                elif expected.get("exists") is False and current.get("sha256"):
                    operation["final_state"] = "changed_after"
            operations.append(operation)
        if row["name"] in PROCESS_TOOLS:
            data = payload.get("data", {})
            state = "not_executed" if row["state"] == "intended" else "unknown"
            if row["state"] == "returned" and effects.get("effects") != "not_executed":
                state = (
                    "timed_out"
                    if data.get("timed_out")
                    else "unknown"
                    if data.get("cleanup_status") == "unknown"
                    else "command_succeeded"
                    if payload.get("success") and data.get("exit_code") == 0
                    else "failed"
                    if payload.get("success") is False or data.get("exit_code") is not None
                    else "unknown"
                )
            if effects.get("effects") == "not_executed":
                state = "not_executed"
            verification = (checks or {}).get(row["call_id"], effects.get("report_check", {}))
            freshness = "unknown"
            if final is not None and verification:
                baseline, end, last = (
                    verification["before"],
                    verification["after"],
                    fingerprint(final),
                )
                if baseline["complete"] and end["complete"] and last["complete"]:
                    freshness = (
                        "unchanged"
                        if baseline["hash"] == end["hash"] == last["hash"]
                        else "needs_recheck"
                    )
            executions.append(
                {
                    "tool": row["name"],
                    "arguments": row["arguments"],
                    "state": state,
                    "freshness": freshness,
                    "exit_code": data.get("exit_code"),
                    "process": {
                        key: value for key, value in data.items() if key not in {"stdout", "stderr"}
                    },
                    "captured_at": verification.get("after", {}).get("at"),
                    "check_snapshots": verification,
                    "output_excerpt": (data.get("stdout", "") + "\n" + data.get("stderr", ""))[
                        :4000
                    ],
                    "ledger": ref,
                }
            )
    return {
        "operations": operations,
        "executions": executions,
        "failures": failures,
        "verification_plan": plans_from_rows(rows),
    }


class ReportCapture:
    def __init__(self, conversation, task, sandbox=None, writeback="manual"):
        started = time.perf_counter()
        self.scan_metrics = []
        self.store = ReportStore(conversation.store)
        self.ledger = conversation.ledger
        self.root = Path(sandbox.workspace if sandbox else conversation.store.project)
        self.exclude = (conversation.store.directory,)
        self.checks = {}
        self.completed_checks = {}
        self.sandbox = sandbox
        self.report = {
            "version": 1,
            "session_id": conversation.store.id,
            "task_id": task["id"],
            "task_number": task["number"],
            "attempt_id": task["attempts"][-1]["id"],
            "retry_of": task.get("retry_of"),
            "continuation_of": task.get("continuation_of"),
            "started_at": now(),
            "state": "incomplete",
            "mode": conversation.mode,
            "writeback_mode": writeback,
            "workspace": str(self.root),
            "project": str(conversation.store.project),
            "baseline": self._snapshot(),
            "unverified": ["需求覆盖与人工验收未自动判定；命令成功不代表全部测试通过。"],
        }
        if sandbox:
            self.report["host_baseline"] = self._snapshot(conversation.store.project)
        self.store.save(self.report)
        self.start_seconds = time.perf_counter() - started

    def _snapshot(self, root=None, *, texts=True):
        snap = snapshot(root or self.root, exclude=self.exclude, texts=texts)
        self.scan_metrics.append(
            {
                "root": snap["root"],
                "texts": texts,
                "complete": snap["complete"],
                **snap.get("metrics", {}),
            }
        )
        return snap

    def before_call(self, call):
        if call.name in PROCESS_TOOLS:
            self.checks[call.id] = fingerprint(self._snapshot(texts=False))

    def after_call(self, call, effects):
        if call.id not in self.checks:
            return effects
        check = {
            "before": self.checks.pop(call.id),
            "after": fingerprint(self._snapshot(texts=False)),
        }
        self.completed_checks[call.id] = check
        return {**effects, "report_check": check}

    def finish(self, state, summary):
        started = time.perf_counter()
        final = self._snapshot()
        self.report.update(
            state=state,
            finished_at=now(),
            outcome=deepcopy(summary),
            final=final,
            changes=changes(self.report["baseline"], final),
            **evidence(self.report, self.ledger, final, checks=self.completed_checks),
        )
        if self.sandbox:
            host = self._snapshot(self.report["project"])
            self.report.update(
                host_final=host, host_changes=changes(self.report["host_baseline"], host)
            )
            self.report["host_verification"] = deepcopy(
                getattr(self.sandbox, "last_verification", None)
            )
        self.report["metrics"] = {
            "start_seconds": self.start_seconds,
            "finish_build_seconds": time.perf_counter() - started,
            "scans": self.scan_metrics,
            "scan_seconds": sum(m.get("scan_seconds", 0) for m in self.scan_metrics),
        }
        return self.store.save(self.report)


def load_report(store, ledger, selector="latest"):
    from .report_reviews import attach_reviews, report_digest

    report = ReportStore(store).get(selector)
    digest = report_digest(report)
    if report["state"] == "incomplete":
        # Recovery is a view of saved evidence, never a retrospective directory scan.
        report.update(evidence(report, ledger))
        report["unverified"].append("结束快照未保存；任务仍在执行或曾异常中断，不能确认最终差异。")
    items = verification_items(report)
    attach_reviews(store, report, items, digest)
    report["revision"] = digest
    report["verification_items"] = items
    return report


def render_report(report, *, detail=False, diff=False):
    states = {
        "completed": "完成",
        "failed": "失败",
        "cancelled": "已取消",
        "needs_input": "待处理",
        "incomplete": "尚未完整保存",
    }
    lines = [
        f"任务交付报告 #{report['task_number']} · {states.get(report['state'], report['state'])}",
        f"环境：{report['mode']} · {report['workspace']}",
    ]
    observed, ops, checks = (
        report.get("changes", []),
        report.get("operations", []),
        report.get("executions", []),
    )
    lines.append(
        f"观察到 {len(observed)} 项文件变化；记录 {len(ops)} 项工具操作；"
        f"{len(checks)} 项命令执行证据"
    )
    failed_checks = sum(c["state"] in {"failed", "timed_out"} for c in checks)
    stale = sum(c["freshness"] == "needs_recheck" for c in checks)
    lines.append(
        f"命令失败/超时 {failed_checks} 项；需重新验证 {stale} 项；"
        f"工具失败/待核实 {len(report.get('failures', []))} 项"
    )
    items = report.get("verification_items", verification_items(report))
    if detail or diff:
        lines.append("\n" + render_verification(items))
    else:
        passed = sum(i["state"] == "passed" for i in items)
        pending = sum(i["state"] in {"not_run", "manual_pending", "unknown"} for i in items)
        lines.append(
            f"验证清单：{len(items)} 项；检查命令通过 {passed} 项；待验证/未确认 {pending} 项"
        )
    if report.get("outcome", {}).get("notice"):
        lines.append(report["outcome"]["notice"])
    if report.get("writeback_mode") and report["mode"] == "docker":
        mode = report["writeback_mode"]
        result = (
            "手动模式；本报告不确认回写"
            if mode == "manual"
            else "自动回写流程完成"
            if report.get("outcome", {}).get("writeback_ok")
            else "自动回写未完成或未知"
        )
        lines.append(f"回写：{mode} · {result}；宿主变化与副本分别记录")
    if not detail and not diff:
        lines.append(
            f"/report {report['task_number']} 查看详情；"
            f"/report {report['task_number']} --diff 查看差异"
        )
    else:
        lines.append("\n任务期间观察到的变化（不能据此归属 Agent）：")
        for change in observed:
            lines.append(f"  {change['path']} · {change['kind']} · 来源未确认")
            if diff:
                lines.append(change["diff"] or f"  差异：{change['diff_status']}")
        lines.append("\n已记录的工具操作（不保证覆盖命令产生的全部变化）：")
        labels = {
            "matches": "最终内容一致",
            "changed_after": "之后存在其他变化",
            "unknown": "最终状态未确认",
        }
        for op in ops:
            lines.append(
                f"  {op['path']} · {labels[op['final_state']]} · {op['ledger']['result_ref']}"
            )
            if diff:
                lines.append(op["diff"] or f"  操作差异：{op['diff_status']}")
        lines.append("\n实际命令与验证证据（退出码 0 仅表示命令成功）：")
        for check in checks:
            labels = {
                "command_succeeded": "命令成功",
                "failed": "失败",
                "timed_out": "超时",
                "unknown": "未确认",
                "not_executed": "尚未执行",
                "unchanged": "检查范围内内容一致",
                "needs_recheck": "文件有变化，需重新验证",
            }
            lines.append(
                f"  {check['tool']} · {labels[check['state']]} · "
                f"{labels[check['freshness']]} · {check['arguments']}"
            )
            lines.append(f"  exit={check['exit_code']} · {check['ledger']['result_ref']}")
            lines.append(check["output_excerpt"])
        for failure in report.get("failures", []):
            lines.append(
                f"失败/待核实：{failure['tool']} · {failure['state']} · {failure['error']}"
                f" · {failure['ledger']}"
            )
        if "host_changes" in report:
            lines.append(
                "\n宿主项目观察到的变化：" + ", ".join(c["path"] for c in report["host_changes"])
            )
            lines.append(
                "最终回写验证：" + str(report.get("host_verification") or "无已记录的验证证据")
            )
    host_check = report.get("host_verification")
    if host_check:
        data = host_check.get("data", {})
        passed = (
            host_check.get("success")
            and data.get("exit_code") == 0
            and not data.get("timed_out")
            and data.get("cleanup_status") != "unknown"
        )
        result = "命令成功" if passed else "未通过或未确认"
        lines.append(f"容器最终验证：{result}；验证有效性未确认，来源为宿主回写流程。")
    if not checks and not host_check:
        lines.append("无已记录的命令验证证据。")
    if detail:
        lines.append("快照为有范围和大小限制的逐文件观察；不是整个目录的原子快照。")
        lines.append("排除目录/文件：" + ", ".join(report["baseline"].get("excluded", [])))
    for key in ("baseline", "final", "host_baseline", "host_final"):
        snap = report.get(key)
        if snap and not snap["complete"]:
            lines.append(f"快照不完整（{key}）：" + "; ".join(snap["issues"][:10]))
    lines.extend(report["unverified"])
    return "\n".join(lines)
