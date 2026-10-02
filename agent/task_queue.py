"""Serializable per-session FIFO state; no execution or presentation dependencies."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

STATES = {"queued", "running", "completed", "failed", "cancelled", "interrupted", "needs_input"}
MAX_TASKS = 1000
MAX_TEXT = 100_000


def now():
    return datetime.now(UTC).isoformat()


def validate_queue(value):
    if value is None:
        return
    try:
        if (
            not isinstance(value, dict)
            or value["version"] != 1
            or type(value["paused"]) is not bool
            or not isinstance(value["reason"], str)
            or type(value["next_number"]) is not int
            or value["next_number"] < 1
            or not isinstance(value["tasks"], list)
            or len(value["tasks"]) > MAX_TASKS
        ):
            raise ValueError
        ids, numbers, active = set(), set(), 0
        for task in value["tasks"]:
            if (
                not isinstance(task, dict)
                or not isinstance(task["id"], str)
                or len(task["id"]) != 32
                or any(c not in "0123456789abcdef" for c in task["id"])
                or task["id"] in ids
                or type(task["number"]) is not int
                or not 0 < task["number"] < value["next_number"]
                or task["number"] in numbers
                or task["state"] not in STATES
                or not isinstance(task["text"], str)
                or not task["text"].strip()
                or len(task["text"]) > MAX_TEXT
                or task["text"].lstrip().startswith("/")
                or not isinstance(task["created_at"], str)
                or not isinstance(task["attempts"], list)
                or len(task["attempts"]) > 1
                or not isinstance(task["result"], dict)
            ):
                raise ValueError
            for attempt in task["attempts"]:
                if (
                    not isinstance(attempt, dict)
                    or not isinstance(attempt["id"], str)
                    or not isinstance(attempt["started_at"], str)
                    or not isinstance(attempt["config"], dict)
                ):
                    raise ValueError
            if task["state"] == "running" and not task["attempts"]:
                raise ValueError
            active += task["state"] == "running"
            ids.add(task["id"])
            numbers.add(task["number"])
        if active > 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("无效的任务队列记录") from None


class TaskQueue:
    def __init__(self, record=None):
        validate_queue(record)
        self.data = (
            deepcopy(record)
            if record is not None
            else {
                "version": 1,
                "paused": False,
                "reason": "",
                "next_number": 1,
                "tasks": [],
            }
        )

    @property
    def pending(self):
        return [task for task in self.data["tasks"] if task["state"] == "queued"]

    @property
    def running(self):
        return next((t for t in self.data["tasks"] if t["state"] == "running"), None)

    @property
    def paused(self):
        return self.data["paused"]

    def snapshot(self):
        return deepcopy(self.data)

    def pause(self, reason):
        self.data.update(paused=True, reason=reason)

    def resume(self):
        self.data.update(paused=False, reason="")

    def recover(self):
        if self.running:
            self.finish(self.running, "interrupted", {"notice": "上次执行未确认，不会自动重跑"})
        if self.pending:
            self.pause("已恢复待执行任务；请检查后使用 /queue resume")

    def get(self, number):
        task = next((t for t in self.data["tasks"] if str(t["number"]) == str(number)), None)
        if task is None:
            raise ValueError("任务编号不存在")
        return task

    @staticmethod
    def check_text(text):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError(f"任务应为非空文本，且不超过 {MAX_TEXT} 字符")
        if text.lstrip().startswith("/"):
            raise ValueError("会话控制命令不能作为任务排队")

    def add(self, text, *, submission_id=None, retry_of=None):
        self.check_text(text)
        key = submission_id or uuid4().hex
        existing = next((t for t in self.data["tasks"] if t["id"] == key), None)
        if existing:
            if existing["text"] != text:
                raise ValueError("重复提交标识对应的任务内容不同")
            return existing
        if len(self.data["tasks"]) >= MAX_TASKS:
            raise ValueError("队列记录已达上限，请启动新会话")
        task = {
            "id": key,
            "number": self.data["next_number"],
            "text": text,
            "state": "queued",
            "created_at": now(),
            "attempts": [],
            "result": {},
            "retry_of": retry_of,
        }
        self.data["next_number"] += 1
        self.data["tasks"].append(task)
        return task

    def edit(self, number, text):
        self.check_text(text)
        task = self.get(number)
        self.require_queued(task)
        task["text"] = text

    @staticmethod
    def require_queued(task):
        if task["state"] != "queued":
            raise ValueError("只能编辑、删除或移动待执行任务")

    def remove(self, number):
        task = self.get(number)
        self.require_queued(task)
        task.update(state="cancelled", result={"notice": "执行前移除"}, finished_at=now())

    def move(self, number, position):
        task = self.get(number)
        self.require_queued(task)
        pending = self.pending
        if not 1 <= position <= len(pending):
            raise ValueError("位置超出待执行队列范围")
        pending.remove(task)
        pending.insert(position - 1, task)
        iterator = iter(pending)
        self.data["tasks"] = [
            next(iterator) if t["state"] == "queued" else t for t in self.data["tasks"]
        ]

    def claim(self, config):
        if self.paused or self.running or not self.pending:
            return None
        task = self.pending[0]
        task["state"] = "running"
        task["attempts"].append(
            {"id": uuid4().hex, "started_at": now(), "config": deepcopy(config)}
        )
        return task

    def finish(self, task, state, result):
        if task["state"] != "running" or state not in STATES - {"running", "queued"}:
            raise ValueError("无效的任务结束状态")
        task.update(state=state, result=deepcopy(result), finished_at=now())
        if state != "completed":
            self.pause(result.get("notice") or f"任务 #{task['number']} {state}")
