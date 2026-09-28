"""Task-boundary writeback policy shared by all CLI presentations."""


def finish_writeback(sandbox, result, mode: str, *, emit=print) -> bool:
    """Called exactly once at a user task boundary, never once per tool."""
    if sandbox is None or mode == "manual":
        return True
    sandbox.last_backup = None
    try:
        if getattr(sandbox.backend, "healthy", True) and not sandbox.changes()[1]:
            # Read-only diagnostics (including expected nonzero exits) have nothing to publish.
            from sandbox.writeback import WritebackGuard

            sandbox.guard = WritebackGuard()
            emit("无需自动回写：没有文件变更。")
            return result.status == "completed"
        reason = sandbox.guard.reason(result, sandbox.backend)
        if reason:
            emit(f"未自动回写：{reason}。副本：{sandbox.workspace}")
            return False
        if sandbox.verify_command:
            emit("正在容器中执行最终验证（时限以当前执行配置为准）……")
        verification_error = sandbox.verify_writeback()
        if verification_error:
            emit(
                f"未自动回写：{verification_error}。"
                f"验证记录：{sandbox.directory / 'verification.json'}；副本：{sandbox.workspace}"
            )
            return False
        changed = sandbox.apply()
    except (ValueError, OSError, KeyboardInterrupt) as error:
        sandbox.guard.needs_review = True
        emit(f"自动回写未完成：{str(error) or '回写被中断'}；副本：{sandbox.workspace}")
        if sandbox.last_backup:
            emit(f"可能已有部分文件写入；恢复备份：{sandbox.last_backup}")
        return False
    if changed:
        emit(f"已自动回写 {len(changed)} 个文件：" + ", ".join(changed))
        emit(f"回写前备份：{sandbox.last_backup}")
        if sandbox.verify_command:
            emit(f"执行检查：已配置的最终验证通过；记录：{sandbox.directory / 'verification.json'}")
        else:
            emit("执行检查：已调用的工具无未解决失败；不代表已执行完整测试。")
    else:
        emit("无需自动回写：没有文件变更。")
    return True
