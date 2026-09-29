"""A small terminal conversation loop; no model client or tool execution is duplicated here."""

from agent import AgentRuntime, RunResult
from host_support.cancellation import RunCancelled, cancellation_scope
from llm import LLMError, Message

from .cancellation import cancellation_notice
from .conversation_help import HELP, RESET_NOTICE, describe_skills
from .input import SessionInput
from .sessions_command import new_name, ui_command
from .thinking_display import ThinkingDisplay
from .writeback import finish_writeback

INPUT_TOP = "┏" + "━" * 18 + " 用户输入 " + "━" * 18
INPUT_BOTTOM = "┗" + "━" * 46


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
        from .terminal.application import ConversationUI

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
                with cancellation_scope(handle_sigint=True):
                    print(conversation.compact())
            except (LLMError, ValueError, OSError) as error:
                print(f"压缩未完成：{error}")
            except (KeyboardInterrupt, RunCancelled):
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
            with cancellation_scope(handle_sigint=True):
                result = runtime.run(task, history=history)
        except (KeyboardInterrupt, RunCancelled) as error:
            if sandbox is not None:
                sandbox.guard.needs_review = True
            history = (
                conversation.fail_task(
                    cancellation=error.report if isinstance(error, RunCancelled) else None
                )
                if conversation
                else ()
            )
            if status:
                status.reset_context()
            notice = (
                "此前完整上下文已保留；继续前请检查文件现状。" if conversation else RESET_NOTICE
            )
            print(
                "\n"
                + (
                    cancellation_notice(error.report)
                    if isinstance(error, RunCancelled)
                    else "当前任务已中断。"
                )
                + notice
            )
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
