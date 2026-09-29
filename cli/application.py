"""Run the assembled CLI application and release resources on every exit path."""

from contextlib import ExitStack
from pathlib import Path

from agent.session import SessionStore
from host_support.cancellation import RunCancelled, cancellation_scope
from tools._internal.web_backend import WebBackend

from .execution_environment import open_execution_environment
from .interactive import display_result, run_interactive
from .models import ModelControl
from .runtime_setup import open_runtime
from .writeback import finish_writeback


def save_before_close(conversation):
    if not conversation.checkpoint():
        raise OSError("会话未能保存，请检查状态目录权限和磁盘空间。")


def run_single_task(args, session, sandbox):
    if sandbox is not None and args.sandbox_writeback == "on-success":
        sandbox.begin_task()
    print(session.thinking.describe())
    session.conversation.start_task(args.task)
    try:
        with cancellation_scope(handle_sigint=True):
            result = session.runtime.run(args.task, history=session.conversation.history)
    except RunCancelled as error:
        if sandbox is not None:
            sandbox.guard.needs_review = True
        session.conversation.fail_task(cancellation=error.report)
        raise
    display_result(result)
    print(session.status.describe())
    writeback_ok = finish_writeback(sandbox, result, args.sandbox_writeback)
    session.conversation.finish_task(result)
    return result.status == "completed" and writeback_ok


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
