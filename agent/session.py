"""Private, atomic project conversations; separate from per-process trace logs."""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from host_support.filesystem import open_file, set_file_mode
from host_support.locking import lock_descriptor
from host_support.paths import session_state_root
from host_support.storage import atomic_write
from llm import LLMError, LLMRequest, Message

SESSION_ID = re.compile(r"[0-9a-f]{32}")
MAX_BYTES = 64 * 1024 * 1024


def _read(path):
    fd = open_file(path)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError("会话记录必须是大小不超过 64 MiB 的独立普通文件")
        return json.loads(stream.read(MAX_BYTES + 1))


def validate_history(values):
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError("无效的会话消息列表")
    history = tuple(Message.from_dict(value) for value in values)
    if history:
        LLMRequest(history)
    for message in history:
        state = message.provider_state
        if state is not None and state.fingerprint != message.fingerprint():
            raise ValueError("会话原生消息状态与正文不匹配")
    return history


def validate_compaction(state, history):
    if state is None:
        return
    try:
        required = {"snapshot", "pins", "prefix", "before", "after"}
        extra = {"target", "target_met", "safety_limit", "auto_retry_at"}
        if not isinstance(state, dict) or set(state) not in (required, required | extra):
            raise ValueError
        if "target" in state:
            if any(type(state[k]) is not int or state[k] <= 0
                   for k in ("target", "safety_limit", "auto_retry_at")):
                raise ValueError
            if (type(state["target_met"]) is not bool
                    or state["target_met"] != (state["after"] <= state["target"])
                    or state["after"] > state["safety_limit"]
                    or state["auto_retry_at"] <= state["after"]):
                raise ValueError
        if not isinstance(state["snapshot"], str) or not SESSION_ID.fullmatch(state["snapshot"]):
            raise ValueError
        if any(type(state[k]) is not int or state[k] <= 0 for k in ("before", "after")):
            raise ValueError
        if state["after"] >= state["before"] or not isinstance(state["pins"], list):
            raise ValueError
        occurrences = set()
        for pin in state["pins"]:
            # Older snapshots without occurrence IDs remain readable.
            if not isinstance(pin, dict) or set(pin) not in (
                {"ref", "text"}, {"ref", "text", "occurrence"}
            ):
                raise ValueError
            if "occurrence" in pin:
                occurrence = pin["occurrence"]
                if (not isinstance(occurrence, str)
                        or not re.fullmatch(r"[0-9a-f]{32}:(0|[1-9][0-9]*)", occurrence)
                        or occurrence in occurrences):
                    raise ValueError
                occurrences.add(occurrence)
            if not isinstance(pin["text"], str) or not isinstance(pin["ref"], str):
                raise ValueError
            if not re.fullmatch(r"[0-9a-f]{32}/m[0-9]+", pin["ref"]):
                raise ValueError
        prefix = validate_history(state["prefix"])
        if len(prefix) != 2 or [m.role for m in prefix] != ["user", "assistant"]:
            raise ValueError
        body = tuple(m for m in validate_history(history) if m.role != "system")
        if body[:2] != prefix:
            raise ValueError
    except (KeyError, TypeError, ValueError, LLMError):
        raise ValueError("会话压缩元数据无效") from None


def validate_loaded_tool_groups(names):
    # Optional v1 field. Store names only; permissions and membership are rebuilt.
    if (not isinstance(names, list) or len(names) > 1024
            or any(not isinstance(name, str)
                   or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("无效的已加载工具组列表")


class SessionStore:
    def __init__(self, project, *, new=False, directory=None, name=None):
        self.project = Path(project).resolve(strict=True)
        key = hashlib.sha256(str(self.project).encode()).hexdigest()
        self.directory = (Path(directory) if directory is not None else session_state_root()) / key
        self.requested_name = validate_name(name) if name is not None else None
        self.new = new
        self.id = uuid4().hex
        self.data = None
        self._lock_fd = None
        self.catalog = SessionCatalog(self.directory, self.project)

    @property
    def label(self):
        row = self.catalog.get(self.id)
        return f"{row['name']} · #{row['sequence']}"

    def open(self):
        # Only the per-project directory is chmod'ed; never change a user's parent directory.
        if self.directory.is_symlink():
            raise ValueError("会话目录不能是符号链接")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self._lock_fd = open_file(
            self.directory / ".lock", os.O_RDWR | os.O_CREAT, nonblocking=False
        )
        try:
            try:
                lock_descriptor(self._lock_fd, blocking=False)
            except BlockingIOError:
                raise ValueError("此项目已有 Agent 会话运行，请退出该会话后再启动") from None
            if not self.new:
                self._load()
            if self.data is not None and self.requested_name is not None:
                raise ValueError("恢复会话时请用 /rename 或 sessions rename；--name 用于新会话")
            self.catalog.register(self.id, name=self.requested_name)
            return self
        except BaseException:
            self.close()
            raise

    def _load(self):
        latest = self.directory / "latest.json"
        if not latest.exists() and not latest.is_symlink():
            return
        try:
            pointer = _read(latest)
            sid = pointer["session_id"]
            if not isinstance(sid, str) or not SESSION_ID.fullmatch(sid):
                raise ValueError
            data = _read(self.directory / f"{sid}.json")
            if (
                data["version"] != 1
                or data["session_id"] != sid
                or data["project"] != str(self.project)
                or not isinstance(data["model"], dict)
                or not isinstance(data["status"], dict)
                or not isinstance(data["transcript"], list)
                or data["mode"] not in {"local", "native", "docker"}
                or type(data.get("sandbox_healthy")) is not bool
                or (
                    data["mode"] == "docker"
                    and (
                        not isinstance(data.get("sandbox"), str)
                        or not Path(data["sandbox"]).is_absolute()
                    )
                )
                or set(data["model"]) != {"provider", "model", "endpoint"}
                or not all(isinstance(v, str) and v for v in data["model"].values())
                or (data["pending_task"] is not None and not isinstance(data["pending_task"], str))
                or type(data["task_number"]) is not int
                or data["task_number"] < 0
            ):
                raise ValueError
            validate_history(data["history"])
            validate_compaction(data.get("compaction"), data["history"])
            validate_loaded_tool_groups(data.get("loaded_tool_groups", []))
            self.id, self.data = sid, data
        except (KeyError, TypeError, ValueError, OSError, LLMError) as error:
            raise ValueError(
                f"上次会话记录无法恢复（{type(error).__name__}）：{self.directory}；"
                "可修复记录，或使用 --new-session 启动新会话，旧记录会保留"
            ) from None

    def _write(self, path, data):
        content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        if len(content) > MAX_BYTES:
            raise ValueError("会话记录超过 64 MiB，无法保存；请启动新会话")
        atomic_write(path, content, prefix=".session-", sync=True)

    def save(self, data):
        if self._lock_fd is None:
            raise ValueError("会话存储尚未打开")
        record = {
            **data,
            "version": 1,
            "session_id": self.id,
            "project": str(self.project),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        validate_history(record["history"])
        validate_compaction(record.get("compaction"), record["history"])
        validate_loaded_tool_groups(record.get("loaded_tool_groups", []))
        with self.catalog.locked() as index:
            metadata = index["sessions"][self.id]
            record.update({key: metadata[key] for key in ("name", "sequence", "created_at")})
            self._write(self.directory / f"{self.id}.json", record)
            metadata["updated_at"] = record["updated_at"]
            index["active_id"] = self.id
            self.catalog.write_index(index)
            self._write(self.directory / "latest.json", {"session_id": self.id})
        self.data = record

    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None


def validate_name(name):
    if not isinstance(name, str):
        raise ValueError("会话名称必须是文字")
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
        raise ValueError("会话名称须为 1–80 个字符，不能包含换行或控制字符")
    return name


def open_log(path, *, write=False, exclusive=False):
    flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND) if write else os.O_RDONLY
    if exclusive:
        flags |= os.O_EXCL
    fd = open_file(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("日志必须是独立普通文件")
        if write:
            set_file_mode(fd, 0o600)
        return os.fdopen(fd, "a" if write else "r", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


class SessionCatalog:
    """Short metadata lock, independent of the long-lived Agent execution lock.

    The index is authoritative for names: an external rename cannot be overwritten
    by an Agent's next checkpoint. Snapshot reads and log readers need no run lock.
    """

    _write = SessionStore._write

    def __init__(self, directory, project):
        self.directory, self.project = Path(directory), Path(project)
        self.warnings = []

    def write_index(self, index):
        self._write(self.directory / "index", index)

    @contextmanager
    def locked(self):
        if self.directory.is_symlink():
            raise ValueError("会话目录不能是符号链接")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_file(self.directory / ".metadata.lock",
                       os.O_RDWR | os.O_CREAT, nonblocking=False)
        try:
            lock_descriptor(fd)
            path = self.directory / "index"
            if path.exists() or path.is_symlink():
                index = _read(path)
                if (not isinstance(index, dict) or index.get("version") != 1
                        or type(index.get("next_sequence")) is not int
                        or not isinstance(index.get("sessions"), dict)):
                    raise ValueError("会话索引损坏，请保留记录并修复 index")
                numbers = set()
                for sid, row in index["sessions"].items():
                    if (not SESSION_ID.fullmatch(sid) or not isinstance(row, dict)
                            or type(row.get("sequence")) is not int or row["sequence"] < 1
                            or row["sequence"] in numbers
                            or not isinstance(row.get("created_at"), str)
                            or not isinstance(row.get("updated_at"), str)):
                        raise ValueError("会话索引损坏")
                    validate_name(row.get("name"))
                    numbers.add(row["sequence"])
                if index["next_sequence"] <= max(numbers, default=0):
                    raise ValueError("会话序号索引损坏")
            else:
                index = {"version": 1, "next_sequence": 1, "sessions": {}}
            changed = False
            self.warnings = []
            # Old snapshots receive numbers once, oldest recorded activity first.
            candidates = [p for p in self.directory.glob("*.json")
                          if SESSION_ID.fullmatch(p.stem) and p.stem not in index["sessions"]]
            for path in sorted(candidates, key=lambda p: (p.lstat().st_mtime, p.name)):
                try:
                    data = _read(path)
                    if data.get("project") != str(self.project) or data.get("session_id") != path.stem:
                        raise ValueError("项目或 ID 不匹配")
                    when = data.get("updated_at")
                    if not isinstance(when, str):
                        raise ValueError("缺少更新时间")
                    self._allocate(index, path.stem, when=when)
                    changed = True
                except (OSError, ValueError, AttributeError) as error:
                    self.warnings.append(f"无法读取旧会话 {path.stem[:8]}：{error}")
            if changed:
                self.write_index(index)
            yield index
        finally:
            os.close(fd)

    def _allocate(self, index, sid, *, name=None, when=None):
        number = index["next_sequence"]
        now = when or datetime.now(UTC).isoformat()
        index["sessions"][sid] = {
            "sequence": number, "name": name or f"会话 {number}",
            "created_at": now, "updated_at": now,
        }
        index["next_sequence"] += 1

    def register(self, sid, *, name=None):
        if not SESSION_ID.fullmatch(sid):
            raise ValueError("无效会话 ID")
        if name is not None:
            name = validate_name(name)
        with self.locked() as index:
            if sid not in index["sessions"]:
                self._allocate(index, sid, name=name)
                self.write_index(index)

    def get(self, sid):
        with self.locked() as index:
            return dict(index["sessions"][sid])

    def latest_id(self):
        try:
            return _read(self.directory / "latest.json")["session_id"]
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            raise ValueError("最后一次会话指针损坏") from None

    def running(self):
        try:
            fd = open_file(self.directory / ".lock", nonblocking=False)
        except FileNotFoundError:
            return False
        try:
            try:
                lock_descriptor(fd, blocking=False)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def entries(self):
        if not self.directory.exists():
            return []
        with self.locked() as index:
            active = index.get("active_id") if self.running() else None
            try:
                latest = self.latest_id()
            except (OSError, ValueError) as error:
                latest = None
                self.warnings.append(f"默认恢复标记不可读：{error}")
            rows = [dict(row, session_id=sid, active=sid == active, latest=sid == latest)
                    for sid, row in index["sessions"].items()
                    if (self.directory / f"{sid}.json").exists()]
        return sorted(rows, key=lambda row: row["sequence"], reverse=True)

    def resolve(self, selector):
        rows = self.entries()
        if selector == "latest":
            matches = [row for row in rows if row["latest"]]
        else:
            matches = [row for row in rows if selector in
                       {str(row["sequence"]), row["session_id"]}]
            if not matches:
                matches = [row for row in rows if row["name"] == selector]
        if not matches:
            raise ValueError(f"找不到会话：{selector}；使用 sessions list 查看")
        if len(matches) != 1:
            raise ValueError("存在同名会话，请使用序号：" +
                             ", ".join(str(row["sequence"]) for row in matches))
        return matches[0]

    def rename(self, sid, name):
        name = validate_name(name)
        with self.locked() as index:
            index["sessions"][sid]["name"] = name
            self.write_index(index)
        return name

    def log_directory(self, sid, *, create=False):
        if not SESSION_ID.fullmatch(sid):
            raise ValueError("无效会话 ID")
        path = self.directory / sid
        if path.is_symlink():
            raise ValueError("日志目录不能是符号链接")
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def log_path(self, sid, kind="chat"):
        if kind not in {"chat", "trace", "jsonl"}:
            raise ValueError("日志类型须为 chat、trace 或 jsonl")
        return self.log_directory(sid) / {"chat": "chat.log", "trace": "trace.log",
                                         "jsonl": "trace.jsonl"}[kind]

    def ensure_chat(self, sid):
        # Bootstrap old visible transcripts once; never append a rendered snapshot
        # on every checkpoint (that would duplicate history and /logs output).
        with self.locked() as index:
            self.log_directory(sid, create=True)
            path = self.log_path(sid)
            if path.exists() or path.is_symlink():
                with open_log(path):
                    pass
                if not index["sessions"][sid].get("chat_created"):
                    index["sessions"][sid]["chat_created"] = True
                    self.write_index(index)
                return path
            if index["sessions"][sid].get("chat_created"):
                raise FileNotFoundError(f"对话日志已丢失，未用快照覆盖重建：{path}")
            snapshot = self.directory / f"{sid}.json"
            text = ""
            if snapshot.exists():
                data = _read(snapshot)
                from agent.transcript import Transcript
                transcript = Transcript.from_records(data["transcript"])
                text = "".join(b.text() for b in transcript.blocks if b.kind != "thinking")
                if text:
                    text = "[从旧会话快照导入；历史执行日志未关联]\n" + text + "\n"
            with open_log(path, write=True, exclusive=True) as stream:
                stream.write(text)
            index["sessions"][sid]["chat_created"] = True
            self.write_index(index)
            return path

    def append_chat(self, sid, text):
        from agent.transcript import display_text
        path = self.ensure_chat(sid)
        with open_log(path, write=True) as stream:
            stream.write(display_text(text))
            stream.flush()
