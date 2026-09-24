"""A small terminal conversation loop; no model client or tool execution is duplicated here."""

from agent import AgentRuntime, RunResult
from llm import LLMError, Message

from .sessions_command import new_name, ui_command
from .live import SessionInput
from .thinking_display import ThinkingDisplay

HELP = (
    "输入任务后按回车。/help 查看帮助，/clear 清空上下文，/new [名称] 启动新会话。"
    "/compact 压缩上下文并归档原文。"
    "/rename 名称 改名；/sessions 列出会话；/logs [序号] --tail 100 查看日志。"
    "/exit、/quit 或 Ctrl+D 退出并保存；下次启动默认恢复，--new-session 启动全新会话。"
    "/model 选择供应商、模型和 API Key，保存并切换。"
    "/skills 查看技能；任务开头用 $技能名 显式指定，也可由模型按需选择。"
    "/context [auto|窗口上限token数] 查看占用、自动获取或手动设置上限。"
    "/thinking list 查看有效档位；/thinking low 等直接切换，history on 保留历史思考，reset 重置。"
    "Shift+Tab 按模型切换并记住偏好；Ctrl+T 展开/折叠思考。"
    "/thinking display collapsed|expanded|hidden 设置并保存显示偏好。"
    "Ctrl+C 取消当前输入或中断任务；执行中要退出可先按 Ctrl+C，再输入 /exit。"
)
RESET_NOTICE = "上下文已清空；已经执行的文件操作不会撤销。"
INPUT_TOP = "┏" + "━" * 18 + " 用户输入 " + "━" * 18
INPUT_BOTTOM = "┗" + "━" * 46


def describe_skills(runtime: AgentRuntime) -> str:
    skills = getattr(runtime, "skills", None)
    return skills.describe() if skills is not None else "当前未启用技能框架。"


def display_result(result: RunResult) -> None:
    if result.undisplayed_text:
        print(result.undisplayed_text)
    if result.status != "completed":
        print(result.notice or f"任务尚未正常完成：{result.status}")


def run_interactive(
    runtime: AgentRuntime,
    *,
    sandbox=None,
    writeback="manual",
    thinking=None,
    status=None,
    models=None,
    display=None,
    conversation=None,
) -> None:
    """Keep completed/pausable histories; discard uncertain failures and cancellations."""
    import sys

    display = display or ThinkingDisplay()
    if sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import ConversationUI

        ConversationUI(
            runtime,
            sandbox=sandbox,
            writeback=writeback,
            thinking=thinking,
            status=status,
            models=models,
            display=display,
            conversation=conversation,
        ).run()
        return
    history: tuple[Message, ...] = conversation.history if conversation else ()
    if conversation:
        text = conversation.transcript.render(display.mode)[0]
        if text:
            print(text)
    reader = SessionInput(thinking, status=status)
    if models:
        print(models.describe())
    if thinking:
        print(thinking.describe())
    while True:
        if conversation:
            print(f"当前会话：{conversation.label}")
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
        if task.split()[0] in {"/rename", "/sessions", "/logs"} and conversation:
            try:
                print(ui_command(conversation, task))
            except (OSError, ValueError) as error:
                print(str(error))
            continue
        if task.split()[0] == "/new" and conversation:
            try:
                print(conversation.new_session(name=new_name(task)))
                history = conversation.history
            except (OSError, ValueError) as error:
                print(str(error))
            continue
        if task == "/compact" and conversation:
            try:
                print(conversation.compact())
            except (LLMError, ValueError, OSError) as error:
                print(f"压缩未完成：{error}")
            except KeyboardInterrupt:
                print("压缩已取消；原上下文保留。")
            history = conversation.history
            continue
        if task == "/clear":
            history = ()
            if status:
                status.reset_context()
            if conversation:
                conversation.clear()
            print(RESET_NOTICE)
            continue
        if task == "/skills":
            print(describe_skills(runtime))
            continue
        if task == "/model" and models:
            from .models import prompt_model

            try:
                selection = prompt_model(models.wizard())
                print(models.switch(selection))
                history = ()
                if conversation:
                    conversation.clear()
            except (KeyboardInterrupt, EOFError):
                print("模型切换已取消，原模型和上下文保留。")
            except (LLMError, ValueError, OSError) as error:
                print(f"模型未切换：{error}")
            continue
        if task.split()[0] == "/context" and status:
            try:
                print(status.context_command(task))
            except (ValueError, OSError, LLMError) as error:
                print(f"设置未变更：{error}")
            continue
        if task.split()[:2] == ["/thinking", "display"]:
            try:
                print(display.command(task))
            except (LLMError, ValueError, OSError) as error:
                print(f"设置未变更：{error}")
            continue
        if task.split()[0] == "/thinking" and thinking:
            try:
                print(thinking.command(task))
            except (LLMError, ValueError, OSError) as error:
                print(f"设置未变更：{error}")
            continue
        if task.startswith("/"):
            print("未知会话命令。" + HELP)
            continue

        try:
            if conversation:
                conversation.start_task(task)
            if sandbox is not None and writeback == "on-success":
                sandbox.begin_task()
            result = runtime.run(task, history=history)
        except KeyboardInterrupt:
            if sandbox is not None:
                sandbox.guard.needs_review = True
            history = conversation.fail_task() if conversation else ()
            if status:
                status.reset_context()
            notice = "此前完整上下文已保留；继续前请检查文件现状。" if conversation else RESET_NOTICE
            print("\n当前任务已中断。" + notice)
            continue
        except (LLMError, ValueError, OSError) as error:
            if sandbox is not None:
                sandbox.guard.needs_review = True
            history = conversation.fail_task() if conversation else ()
            if status:
                status.reset_context()
            print(f"{type(error).__name__}: {error}")
            stats = getattr(runtime, "last_stats", None)
            if stats and any(c.first_display_seconds is not None for c in stats.model_calls):
                print("上方输出可能不完整，本次任务未完成。")
            print("此前完整上下文已保留；继续前请检查文件现状。" if conversation else RESET_NOTICE)
            continue

        display_result(result)
        finish_writeback(sandbox, result, writeback)
        if conversation:
            history = conversation.finish_task(result)
        elif result.status == "stopped" and not result.resumable:
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
