"""Resolve session permissions before creating execution backends or project files."""

import json
from pathlib import Path


def guard_state_location(store, requested):
    if not store.directory.resolve().is_relative_to(store.project):
        return
    # Inspect without opening the store: open() repairs metadata and takes locks.
    # A review snapshot in an invalid state location must also fail on plain resume.
    review = requested == "review"
    candidates = list(store.directory.glob("*.json"))
    pending = store.directory / ".pending-save"
    if pending.exists():
        candidates.append(pending)
    for path in candidates:
        if path.stem == "latest":
            continue
        try:
            data = json.loads(path.read_text())
            review |= isinstance(data, dict) and data.get("access_mode") == "review"
        except (OSError, ValueError):
            if requested != "develop":
                review = True  # Unknown persisted permissions cannot authorize a write.
    if review:
        raise ValueError("只读审查的状态目录必须位于项目外；请调整 XDG_STATE_HOME")


def resolve_mode(store, requested):
    saved = (store.data or {}).get("access_mode", "develop")
    if store.data is not None and requested is not None and requested != saved:
        raise ValueError("会话权限模式不能改变；请使用 --new-session --mode " + requested)
    mode = requested or saved
    if mode not in {"review", "develop"}:
        raise ValueError("无效的会话权限模式")
    return mode


def review_log_directory(store, root):
    path = store.directory / "review-logs"
    if path.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("审查日志必须保存在被审查项目外")
    return path
