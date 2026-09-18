"""A small terminal conversation loop; no model client or tool execution is duplicated here."""

from agent import AgentRuntime, RunResult
from llm import LLMError, Message

from .live import SessionInput

HELP = (
    "输入任务后按回车。/help 查看帮助，/clear 清空上下文，/exit、/quit 或 Ctrl+D 退出。"
    "/skills 查看技能；任务开头用 $技能名 显式指定，也可由模型按需选择。"
    "/context [窗口上限token数] 查看上下文占用或设置模型上限。"
    "/thinking [auto|off|on|adaptive] [强度] [budget=整数]；Shift+Tab 在输入时切换思考预设。"
    "Ctrl+C 取消当前输入或中断任务；执行中要退出可先按 Ctrl+C，再输入 /exit。"
)
RESET_NOTICE = "上下文已清空；已经执行的文件操作不会撤销。"
INPUT_TOP = "┏" + "━" * 18 + " 用户输入 " + "━" * 18
INPUT_BOTTOM = "┗" + "━" * 46


def describe_skills(runtime: AgentRuntime) -> str:
    skills = getattr(runtime, "skills", None)
    return skills.describe() if skills is not None else "当前未启用技能框架。"


def display_result(result: RunResult) -> None:
    displayed = bool(
        result.stats
        and result.stats.model_calls
        and result.stats.model_calls[-1].first_display_seconds is not None
    )
    if result.text and not displayed:
        print(result.text)
    if result.status != "completed":
        reason = "达到轮数上限" if result.status == "max_steps" else result.response.finish_reason
        print(f"任务尚未正常完成：{reason}")


def run_interactive(
    runtime: AgentRuntime, *, sandbox=None, writeback="manual", thinking=None, status=None
) -> None:
    """Keep successful histories in memory. Discard uncertain turns after failure/cancellation."""
    import sys

    if sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import ConversationUI

        ConversationUI(
            runtime, sandbox=sandbox, writeback=writeback, thinking=thinking, status=status
        ).run()
        return
    history: tuple[Message, ...] = ()
    reader = SessionInput(thinking, status=status)
    if thinking:
        print(thinking.describe())
    while True:
        if status and reader.session is None:
            print(status.describe())
        print("\n" + INPUT_TOP, flush=True)
        try:
            task = reader.read("你> ").strip()
        except EOFError:
            print("\n" + INPUT_BOTTOM)
            print("\n会话已结束。")
            return
        except KeyboardInterrupt:
            print("\n" + INPUT_BOTTOM)
            print("\n输入已取消，输入 /exit 退出。")
            continue
        print(INPUT_BOTTOM, flush=True)
        if not task:
            continue
        if task in {"/exit", "/quit"}:
            print("会话已结束。")
            return
        if sandbox is not None and task in {"/diff", "/apply"}:
            try:
                print(
                    sandbox.diff() if task == "/diff" else f"已回写 {len(sandbox.apply())} 个文件。"
                )
                if task == "/apply" and sandbox.last_backup:
                    print(f"回写前备份：{sandbox.last_backup}")
            except (ValueError, OSError) as error:
                print(f"回写或检查失败：{error}")
            continue
        if task == "/help":
            if sandbox is not None:
                print("/diff 查看副本变更；/apply 显式回写。退出后副本会保留。")
            print(HELP)
            continue
        if task == "/clear":
            history = ()
            if status:
                status.reset_context()
            print(RESET_NOTICE)
            continue
        if task == "/skills":
            print(describe_skills(runtime))
            continue
        if task.split()[0] == "/context" and status:
            try:
                print(status.context_command(task))
            except (ValueError, LLMError) as error:
                print(f"设置未变更：{error}")
            continue
        if task.split()[0] == "/thinking" and thinking:
            try:
                thinking.command(task)
                print(thinking.describe())
                print("会话设置已更新，下一次请求生效；模型是否支持以服务端为准。")
            except (LLMError, ValueError) as error:
                print(f"设置未变更：{error}")
            continue
        if task.startswith("/"):
            print("未知会话命令。" + HELP)
            continue

        try:
            if sandbox is not None and writeback == "on-success":
                sandbox.begin_task()
            result = runtime.run(task, history=history)
        except KeyboardInterrupt:
            if sandbox is not None:
                sandbox.guard.needs_review = True
            history = ()
            if status:
                status.reset_context()
            print("\n当前任务已中断。" + RESET_NOTICE)
            continue
        except (LLMError, ValueError, OSError) as error:
            if sandbox is not None:
                sandbox.guard.needs_review = True
            history = ()
            if status:
                status.reset_context()
            print(f"{type(error).__name__}: {error}")
            stats = getattr(runtime, "last_stats", None)
            if stats and any(c.first_display_seconds is not None for c in stats.model_calls):
                print("上方输出可能不完整，本次任务未完成。")
            print(RESET_NOTICE)
            continue

        display_result(result)
        finish_writeback(sandbox, result, writeback)
        if result.status == "stopped":
            # A truncated/blocked reply may carry incomplete tool calls or provider state.
            history = ()
            if status:
                status.reset_context()
            print(RESET_NOTICE)
        else:
            # max_steps also has paired tool results, so a follow-up can safely continue.
            history = result.history


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
            emit("正在容器中执行最终验证（最多 120 秒）……")
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
