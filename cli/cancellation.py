"""User-facing cancellation notices, separate from execution control flow."""


def cancellation_notice(report):
    cleanup = {
        "not_needed": "无需额外进程清理。",
        "confirmed": "已确认所跟踪进程的清理。",
        "unknown": "进程清理尚未确认；请检查后再继续。",
    }[report["cleanup_status"]]
    effects = "已执行的操作不会自动回滚；请检查文件现状。" if report["tools"] else ""
    paths = []
    for change in report["changes"]:
        result = change["result"]
        path = result.get("path") or result.get("destination")
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    changed = "已报告的文件操作：" + "、".join(paths[:5]) + "。" if paths else ""
    return f"当前任务已中断（用户主动停止）。已显示的输出可能不完整。{cleanup}{changed}{effects}"
