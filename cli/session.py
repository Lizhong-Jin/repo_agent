"""Coordinate a saved conversation with either terminal UI and the sandbox."""

import hashlib
from dataclasses import replace
from uuid import uuid4

from agent.compaction import CompactionSettings, ContextCompactor, compaction_notice
from agent.history import HistoryArchive
from agent.session import validate_name
from llm import Message
from llm.providers import get_provider

from .transcript import Transcript


def model_identity(config):
    provider = get_provider(config.provider)
    base = (config.base_url or provider.base_url).rstrip("/")
    return {
        "provider": provider.name,
        "model": config.model,
        "endpoint": hashlib.sha256(base.encode()).hexdigest(),
    }


class SavedConversation:
    def __init__(self, store, runtime, config, status, *, sandbox=None, restore_window=True,
                 tracer=None, execution_mode=None, execution_backend=None, compaction_settings=None):
        self.store, self.runtime, self.config, self.status = store, runtime, config, status
        self.tracer = tracer
        self.log_error = None
        self.sandbox = sandbox
        self.mode = execution_mode or ("docker" if sandbox is not None else "local")
        self.execution_backend = execution_backend
        self.history = ()
        self.transcript = Transcript()
        self.pending_task = None
        self.save_error = None
        self.compaction_state = None
        self.archive = HistoryArchive(store)
        self.compactor = ContextCompactor(
            self, self.archive, compaction_settings or CompactionSettings(auto=False)
        )
        runtime.before_request = self.compactor.before_request
        data = store.data
        self.notice = f"新会话：{store.label}"
        if data is not None:
            self.compaction_state = data.get("compaction")
            if self.compaction_state:
                self.archive.check_snapshot(self.compaction_state["snapshot"])
            self.history = tuple(Message.from_dict(m) for m in data["history"])
            self.transcript = Transcript.from_records(data["transcript"])
            same_model = data["model"] == model_identity(config)
            system = tuple(m for m in self.history if m.role == "system")
            current = (Message("system", runtime.system_prompt),) if runtime.system_prompt else ()
            same_prompt = system == current
            if self.history:
                self.history = current + tuple(m for m in self.history if m.role != "system")
            if not same_model:
                self.history = tuple(replace(m, provider_state=None) for m in self.history)
            status.restore_session(
                data["status"],
                context=same_model and same_prompt,
                restore_window=restore_window and same_model,
            )
            runtime._task_number = data["task_number"]
            self.notice = (
                f"已恢复此项目上次会话：{store.label}（{len(self.history)} 条上下文消息）"
            )
            if not same_model:
                self.notice += "；模型或地址已变化，保留消息并移除旧模型原生状态"
            if data["mode"] != self.mode:
                self.history += (
                    Message(
                        "user",
                        "[会话恢复说明] 执行环境已切换。"
                        "之前沙箱中未回写的修改不代表已存在于当前工作目录。"
                        "请先读取当前文件并核实状态，再继续任务。",
                    ),
                )
                status.reset_context()
                self.notice += "；执行环境已切换，请核实文件现状"
            self.pending_task = data["pending_task"]
            if self.pending_task is not None:
                self._interrupted()
                self.notice += "；上次任务未完成，未自动重跑"

        status.initialize_context(runtime, self.history)
        self._chat(f"\n[{'恢复' if data else '开始'}会话：{store.label}]\n")

    @property
    def label(self):
        return self.store.label

    def _chat(self, text):
        try:
            self.store.catalog.append_chat(self.store.id, text)
        except (OSError, ValueError) as error:
            self.log_error = f"对话日志保存失败：{type(error).__name__}；请检查日志目录"

    def rename(self, name):
        self.store.catalog.rename(self.store.id, name)
        self._chat(f"[会话改名：{self.label}]\n")
        return f"会话名称已更新：{self.label}"

    def _config(self):
        return getattr(self.runtime.llm, "config", self.config)

    def _record(self):
        return {
            "history": [m.to_dict() for m in self.history],
            "compaction": self.compaction_state,
            "transcript": self.transcript.to_records(),
            "model": model_identity(self._config()),
            "status": self.status.session_state(),
            "task_number": self.runtime._task_number,
            "mode": self.mode,
            "sandbox": str(self.sandbox.directory) if self.sandbox is not None else None,
            "pending_task": self.pending_task,
            "sandbox_healthy": getattr(
                self.execution_backend or getattr(self.sandbox, "backend", None), "healthy", True
            ),
        }

    def checkpoint(self, *, history=None, transcript=None, strict=False, emit=print):
        if history is not None:
            self.history = tuple(history)
        if transcript is not None:
            self.transcript = transcript
        try:
            self.store.save(self._record())
            self.save_error = None
            if self.log_error:
                emit(self.log_error)
            return True
        except (OSError, ValueError) as error:
            self.save_error = f"会话保存失败（{type(error).__name__}）：{self.store.directory}"
            if strict:
                raise OSError(self.save_error + "；尚未执行新任务") from None
            emit(self.save_error + "；已执行的文件操作不会撤销，退出时将重试")
            return False

    def start_task(self, task, *, transcript=None):
        if transcript is None:
            self.transcript.append(f"\n你> {task}\n", kind="user")
        else:
            self.transcript = transcript
        self.pending_task = task
        try:
            self.checkpoint(strict=True)
            self._chat(f"\n你> {task}\n")
        except OSError:
            self.pending_task = None
            raise

    def finish_task(self, result, *, transcript=None, emit=print):
        for response in getattr(result, "responses", ()) or (result.response,):
            if response.text:
                self._chat(f"\nAgent> {response.text}\n")
        self._chat(f"[任务结束：{result.status}]\n")
        if result.status == "stopped" and not getattr(result, "resumable", False):
            self._interrupted()
        else:
            self.history = tuple(result.history)
            self.pending_task = None
        if transcript is None:
            for response in getattr(result, "responses", ()) or (result.response,):
                if response.text:
                    self.transcript.append(response.text + "\n")
        self.checkpoint(transcript=transcript, emit=emit)
        return self.history

    def _interrupted(self):
        if self.pending_task is not None:
            self.history += (
                Message(
                    "user",
                    "[会话恢复说明] 上次任务未完整结束：\n"
                    + self.pending_task
                    + "\n部分文件操作可能已经执行，不能假设已回滚或全部成功。"
                    "请先检查文件与工具执行结果，再根据用户的新请求继续；不要自动重跑命令。",
                ),
            )
        self.pending_task = None
        self.status.reset_context()

    def fail_task(self, *, transcript=None, emit=print):
        self._chat("[任务未完成；部分操作可能已经执行，请核实文件状态]\n")
        self._interrupted()
        self.checkpoint(transcript=transcript, emit=emit)
        return self.history

    def commit_compaction(self, history, state):
        """Persist the replacement before exposing it to the next model request."""
        old_history, old_state = self.history, self.compaction_state
        self.history, self.compaction_state = tuple(history), state
        self.status.reset_context()
        self.status.initialize_context(self.runtime, self.history)
        try:
            self.store.save(self._record())
        except BaseException:
            self.history, self.compaction_state = old_history, old_state
            self.status.reset_context()
            self.status.initialize_context(self.runtime, old_history)
            raise
        self._chat(f"[{compaction_notice(state)}]\n")

    def compact(self):
        try:
            self.compactor.compact()
        finally:
            # Account for successful API calls even if validation/cancellation failed.
            self.checkpoint()
        state = self.compaction_state
        return compaction_notice(state)

    def clear(self, *, transcript=None, emit=print):
        self.compaction_state = None
        self._chat("[上下文已清空；对话记录保留]\n")
        self.history = ()
        self.pending_task = None
        self.status.reset_context()
        self.checkpoint(transcript=transcript, emit=emit)

    def new_session(self, *, name=None, transcript=None, emit=print):
        if name is not None:
            name = validate_name(name)
        self.checkpoint(transcript=transcript, strict=True)
        old_id, old_data = self.store.id, self.store.data
        record = self._record()
        record.update(history=[], transcript=[], pending_task=None, compaction=None)
        record["status"].update(
            calls=0,
            totals={"input_tokens": 0, "output_tokens": 0},
            reported={"input_tokens": 0, "output_tokens": 0},
            context_tokens=None,
            context_input_tokens=None,
            context_cached_input_tokens=None,
            context_note="待请求",
        )
        self.store.id = uuid4().hex
        try:
            self.store.catalog.register(self.store.id, name=name)
            self.store.save(record)
        except (OSError, ValueError):
            self.store.id, self.store.data = old_id, old_data
            raise OSError("新会话保存失败；当前会话保留") from None
        if self.tracer is not None:
            self.tracer.switch_session()
        self.log_error = None
        self._chat(f"[开始会话：{self.label}]\n")
        self.history = ()
        self.pending_task = None
        self.transcript = Transcript()
        self.status.reset_session()
        self.compaction_state = None
        self.notice = f"已启动新会话：{self.label}；文件修改保留"
        return self.notice
