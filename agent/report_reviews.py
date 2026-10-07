"""Explicit UI/CLI acceptance, separate from immutable machine execution evidence."""

import hashlib
import json
import os
import stat
from datetime import UTC, datetime

from host_support.filesystem import open_file
from host_support.storage import atomic_write, sync_directory

MAX_BYTES = 1024 * 1024


def report_digest(report):
    return hashlib.sha256(
        json.dumps(report, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def review_path(store, report):
    return store.directory / "reports" / store.id / f"reviews-{report['attempt_id']}.json"


def read_reviews(path):
    try:
        fd = open_file(path)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError("验收记录必须是大小受限的独立普通文件")
        value = json.loads(stream.read(MAX_BYTES + 1))
    if not isinstance(value, list) or any(
        not isinstance(r, dict)
        or set(r) != {"check_id", "state", "note", "at", "report_digest"}
        or r["state"] not in {"pending", "accepted", "rejected"}
        or not all(isinstance(v, str) for v in r.values())
        for r in value
    ):
        raise ValueError("验收记录损坏")
    return value


def attach_reviews(store, report, items, digest):
    reviews = read_reviews(review_path(store, report))
    for item in items:
        matched = [
            r for r in reviews if r["check_id"] == item["id"] and r["report_digest"] == digest
        ]
        if matched:
            item["acceptance"] = {**matched[-1], "source": "user_interface"}
        item["review_history"] = [r for r in reviews if r["check_id"] == item["id"]]


def record_review(store, selector, check_id, state, note, *, expected_digest=None):
    # Imported here to keep the report projection independent of mutation entrypoints.
    from .task_reports import ReportStore
    from .verification import verification_items

    if (
        state not in {"pending", "accepted", "rejected"}
        or not isinstance(note, str)
        or not 1 <= len(note.strip()) <= 2000
    ):
        raise ValueError("验收状态必须是 accepted/rejected/pending，并提供 1–2000 字符备注")
    with store.catalog.locked():
        report = ReportStore(store).get(selector)
        if expected_digest is not None and report_digest(report) != expected_digest:
            raise ValueError("报告内容已变化，请刷新后重新验收")
        if report["state"] == "incomplete":
            raise ValueError("任务报告尚未完整保存，不能提交验收结论")
        if check_id not in {item["id"] for item in verification_items(report)}:
            raise ValueError("验证项目不存在")
        path = review_path(store, report)
        values = read_reviews(path)
        values.append(
            {
                "check_id": check_id,
                "state": state,
                "note": note.strip(),
                "at": datetime.now(UTC).isoformat(),
                "report_digest": report_digest(report),
            }
        )
        payload = json.dumps(values, ensure_ascii=False).encode()
        if len(payload) > MAX_BYTES:
            raise ValueError("验收记录达到大小上限")
        atomic_write(path, payload, mode=0o600, sync=True)
        sync_directory(path.parent)
    return f"已记录任务 #{report['task_number']} 的验收意见：{check_id} · {state}"
