"""A small terminal conversation loop; no model client or tool execution is duplicated here."""

from agent import AgentRuntime, RunResult
from host_support.cancellation import RunCancelled, cancellation_scope
from llm import LLMError

from .conversation_help import HELP, RESET_NOTICE, describe_skills
from .input import SessionInput
from .sessions_command import new_name, ui_command
from .task_controller import TaskController
from .thinking_display import ThinkingDisplay

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
    controller = TaskController(
        runtime,
        conversation=conversation,
        sandbox=sandbox,
        writeback=writeback,
        status=status,
    )
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
        while True:
            try:
                queued = controller.start_next()
                if queued is None:
                    break
                print(f"执行队列 #{queued['number']}：{queued['text']}")
                outcome = controller.execute(queued)
                state = controller.finish(queued, outcome)
                if state != "completed":
                    print(controller.queue.data["reason"])
                    print("队列已暂停；/queue 查看，/queue resume 继续。")
            except (OSError, ValueError) as error:
                print(str(error))
                break
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
        if task.split()[0] == "/queue":
            try:
                print(controller.command(task))
            except (OSError, ValueError) as error:
                print(str(error))
            continue
        if task.split()[0] in {
            "/model",
            "/new",
            "/clear",
            "/apply",
            "/compact",
            "/thinking",
            "/context",
        }:
            try:
                controller.require_idle_configuration()
            except ValueError as error:
                print(str(error))
                continue
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
        if task.split()[0] in {"/rename", "/sessions", "/logs", "/ledger"} and conversation:
            try:
                print(ui_command(conversation, task))
            except (OSError, ValueError) as error:
                print(str(error))
            continue
        if task.split()[0] == "/new" and conversation:
            try:
                print(conversation.new_session(name=new_name(task)))
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
            continue
        if task == "/clear":
            controller.reset_history()
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
                controller.reset_history()
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
            queued = controller.enqueue(task)
            print(f"已加入队列 #{queued['number']}")
        except (OSError, ValueError) as error:
            print(str(error))
