"""Durable execution evidence, independent of diagnostic logs and chat snapshots."""

import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock

from host_support.execution_receipt import PersistenceError
from host_support.filesystem import open_file, set_file_mode


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class ExecutionLedger:
    def __init__(self, store):
        self._lock = RLock()
        self.store = store
        self.path = (store.directory / "execution.sqlite3").absolute()
        self.required = bool((store.data or {}).get("ledger_cursor", 0))

    @contextmanager
    def connect(self, *, write=False):
        with self._lock:
            with self._connect(write=write) as db:
                yield db

    @contextmanager
    def _connect(self, *, write=False):
        db = None
        try:
            if self.required and not self.path.exists():
                raise OSError("执行账本缺失")
            fd = open_file(self.path, os.O_RDWR | os.O_CREAT if write else os.O_RDONLY)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("执行账本必须为独立普通文件")
                if write:
                    set_file_mode(fd, 0o600)
            finally:
                os.close(fd)
            db = sqlite3.connect(
                self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True
            )
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("不支持的执行账本版本")
            if write:
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS runs (
                        id TEXT PRIMARY KEY, session TEXT NOT NULL, identity TEXT NOT NULL,
                        created TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS calls (
                        run TEXT NOT NULL, call_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        name TEXT NOT NULL, arguments TEXT NOT NULL, assistant TEXT NOT NULL,
                        state TEXT NOT NULL, observation TEXT, effects TEXT,
                        PRIMARY KEY(run, call_id));
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                        run TEXT NOT NULL, call_id TEXT, phase TEXT NOT NULL,
                        created TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS events_session ON events(session, seq);
                """)
                db.execute("PRAGMA user_version=1")
                db.execute("BEGIN IMMEDIATE")
            else:
                db.execute("PRAGMA query_only=ON")
            yield db
            if write:
                db.commit()
                self.required = True
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            raise PersistenceError(
                f"执行账本保存或读取失败：{type(error).__name__}；必须停止执行"
            ) from error
        finally:
            if db is not None:
                db.close()

    def _event(self, db, run, call, phase):
        db.execute(
            "INSERT INTO events(session,run,call_id,phase,created) VALUES(?,?,?,?,?)",
            (
                self.store.id,
                run,
                call,
                phase,
                datetime.now(UTC).isoformat(),
            ),
        )

    def begin(self, run, identity):
        with self.connect(write=True) as db:
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?)",
                (
                    run,
                    self.store.id,
                    encoded(identity),
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._event(db, run, None, "run_started")

    def prepare(self, run, assistant):
        # Save the whole validated batch before executing its first member.
        with self.connect(write=True) as db:
            ordinal = db.execute("SELECT COUNT(*) FROM calls WHERE run=?", (run,)).fetchone()[0]
            for call in assistant.tool_calls:
                ordinal += 1
                db.execute(
                    "INSERT INTO calls VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        run,
                        call.id,
                        ordinal,
                        call.name,
                        encoded(call.arguments),
                        encoded(assistant.to_dict()),
                        "intended",
                    ),
                )
                self._event(db, run, call.id, "intended")

    def start(self, run, call):
        with self.connect(write=True) as db:
            changed = db.execute(
                "UPDATE calls SET state='started' WHERE run=? AND call_id=? AND state='intended'",
                (run, call),
            ).rowcount
            if changed != 1:
                raise ValueError("工具开始状态冲突")
            self._event(db, run, call, "started")

    def finish(self, run, call, observation, effects, *, certain=True):
        with self.connect(write=True) as db:
            state = "returned" if certain else "uncertain"
            changed = db.execute(
                "UPDATE calls SET state=?,observation=?,effects=? "
                "WHERE run=? AND call_id=? AND state='started'",
                (
                    state,
                    encoded(observation.to_dict()),
                    encoded(effects),
                    run,
                    call,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("工具结束状态冲突")
            self._event(db, run, call, state)

    def watermark(self):
        if not self.path.exists() and not self.required:
            return 0
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE session=?", (self.store.id,)
            ).fetchone()[0]

    def evidence(self, *, after=0, limit=100, before=None):
        if not self.path.exists() and not self.required:
            return []
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT c.*, r.identity, MAX(e.seq) AS seq FROM calls c
                JOIN runs r ON r.id=c.run
                JOIN events e ON e.run=c.run AND e.call_id=c.call_id
                WHERE r.session=? GROUP BY c.run,c.call_id HAVING MAX(e.seq)>? AND MAX(e.seq)<?
                ORDER BY seq DESC LIMIT ?
            """,
                (self.store.id, after, before if before is not None else 2**63 - 1, limit),
            ).fetchall()
            return [
                {
                    **dict(row),
                    **{
                        key: json.loads(row[key]) if row[key] else None
                        for key in ("arguments", "assistant", "observation", "effects", "identity")
                    },
                }
                for row in rows
            ]

    def describe(self, *, after=0, limit=30, before=None):
        rows = self.evidence(after=after, limit=limit, before=before)
        if not rows:
            return "执行账本暂无工具记录。"
        labels = {
            "intended": "尚未调用",
            "started": "结果待核实",
            "uncertain": "结果待核实",
            "returned": "返回结果已保存",
        }
        lines = ["执行证据（最近记录；不表示文件已回滚或所有外部效果均已核实）："]
        for row in rows:
            effects = dict(row["effects"] or {})
            effects["targets"] = {
                key: value
                for key, value in row["arguments"].items()
                if key in {"path", "source", "destination", "cwd"}
            }
            details = encoded(effects)
            observation = row["observation"]
            outcome = ""
            if observation:
                outcome = " · 工具报告失败" if observation.get("is_error") else " · 工具报告成功"
            lines.append(
                f"#{row['seq']} {row['name']} · {labels[row['state']]}{outcome}\n"
                f"  run={row['run']} call={row['call_id']}\n  {details[:1200]}"
            )
        return "\n".join(lines)
