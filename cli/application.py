"""Run the assembled CLI application and release resources on every exit path."""

from contextlib import ExitStack
from pathlib import Path

from agent.session import SessionStore
from tools._internal.web_backend import WebBackend

from .execution_environment import open_execution_environment
from .interactive import run_interactive
from .models import ModelControl
from .runtime_setup import open_runtime
from .task_controller import TaskController


def save_before_close(conversation):
    if not conversation.checkpoint():
        raise OSError("会话未能保存，请检查状态目录权限和磁盘空间。")


def run_single_task(args, session, sandbox):
    controller = TaskController(
        session.runtime,
        conversation=session.conversation,
        sandbox=sandbox,
        writeback=args.sandbox_writeback,
        status=session.status,
    )
    if controller.queue.pending:
        raise ValueError("已有待执行队列；请进入交互模式处理，或使用 --new-session")
    controller.resume()  # --task is an explicit request; never resumes older queued work.
    print(session.thinking.describe())
    controller.enqueue(args.task)
    task = controller.start_next()
    if task is None:
        return False
    outcome = controller.execute(task)
    state = controller.finish(task, outcome)
    print(session.status.describe())
    if outcome.kind == "cancelled":
        raise outcome.value
    if state != "completed":
        print(controller.queue.data["reason"])
    return state == "completed"


def run_application(args, capabilities):
    """Return success; resource teardown also runs on SystemExit and KeyboardInterrupt."""
    workspace_root = Path(args.root).resolve(strict=True)
    with ExitStack() as resources:
        web = WebBackend.from_environment()
        if web is not None:
            resources.callback(web.close)
        store = SessionStore(workspace_root, new=args.new_session, name=args.name).open()
        resources.callback(store.close)
        environment = open_execution_environment(
            args, workspace_root, store, capabilities, resources
        )
        with open_runtime(args, workspace_root, store, environment, web) as session:
            # A checkpoint runs while store, trace and execution backend are still alive.
            with ExitStack() as session_resources:
                session_resources.callback(save_before_close, session.conversation)
                session.conversation.checkpoint(strict=True)
                print(session.conversation.notice)
                if args.task is not None:
                    return run_single_task(args, session, environment.sandbox)
                models = ModelControl(
                    session.runtime,
                    session.config,
                    thinking=session.thinking,
                    status=session.status,
                    tracer=session.tracer,
                    client_factory=session.client_factory,
                )
                session_resources.callback(models.close)
                options = {}
                if environment.sandbox is not None:
                    options.update(sandbox=environment.sandbox, writeback=args.sandbox_writeback)
                run_interactive(
                    session.runtime,
                    thinking=session.thinking,
                    status=session.status,
                    models=models,
                    display=session.display,
                    conversation=session.conversation,
                    **options,
                )
                return True
