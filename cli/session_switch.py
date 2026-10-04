"""Validated UI handoff; application composition owns runtime/environment replacement."""

import json
import shlex
from copy import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionSwitch:
    session_id: str


def prepare_switch(conversation, controller, command):
    words = shlex.split(command)
    selector = " ".join(words[1:]).strip()
    if not selector:
        raise ValueError("用法：/switch 会话序号、完整 ID 或名称；/sessions 查看列表")
    with conversation.state_lock:
        if controller.active or controller.closing:
            raise ValueError("请等待当前任务执行和收尾完成后再切换会话")
        if controller.cleanup_blocked or not controller._healthy():
            raise ValueError("进程清理尚未确认；请先退出并检查进程，暂不能切换会话")
        sid, _ = conversation.store.read_session(selector)
        if sid == conversation.store.id:
            return None
        if controller.queue.pending and not controller.queue.paused:
            raise ValueError("存在待执行任务；请先使用 /queue pause 暂停队列后再切换")
        conversation.checkpoint(strict=True)
        return SessionSwitch(sid)


def continuation_args(args, session):
    """Keep the live model/thinking policy; session snapshots never restore credentials."""
    result = copy(args)
    config = session.runtime.llm.config
    result.provider, result.model = config.provider, config.model
    result.api_key, result.base_url = config.api_key, config.base_url
    result.new_session, result.name = False, None
    thinking = session.thinking
    result.thinking = thinking.current["mode"]
    result.reasoning_effort = thinking.current["effort"]
    result.thinking_budget = thinking.current["budget"]
    result.thinking_history = thinking.current["history"]
    result.thinking_profile = json.dumps(thinking.override)
    result.extra_json = json.dumps(thinking.base)
    result.thinking_explicit = True
    result.max_output_tokens = session.runtime.max_output_tokens
    result.thinking_display = session.display.mode
    return result
