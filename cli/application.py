"""Run the assembled CLI application and release resources on every exit path."""

import subprocess
from contextlib import ExitStack
from pathlib import Path

from agent.session import SessionStore
from agent.workspaces import prepare_workspace
from llm import LLMError
from tools._internal.web_backend import WebBackend

from .execution_environment import open_execution_environment
from .interactive import run_interactive
from .models import ModelControl
from .runtime_setup import open_runtime
from .session_switch import SessionSwitch, continuation_args
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
    """Keep the project lock while replacing a session's complete runtime and environment."""
    workspace_root = Path(args.root).resolve(strict=True)
    with ExitStack() as resources:
        web = WebBackend.from_environment()
        if web is not None:
            resources.callback(web.close)
        store = SessionStore(
            workspace_root,
            new=args.new_session,
            name=args.name,
            session=getattr(args, "session", None),
        ).open()
        resources.callback(store.close)
        selector = fallback_id = None
        while True:
            try:
                if selector is not None:
                    store.select(selector)
                # Old models, callbacks and native tools are closed before switching IDs.
                with ExitStack() as session_resources:
                    workspace = prepare_workspace(store, getattr(args, "workspace", None))
                    if workspace is not None and args.sandbox == "docker":
                        raise ValueError("独立工作区会话不能切换到 Docker；请使用 --new-session")
                    execution_root = workspace.root if workspace is not None else workspace_root
                    environment = open_execution_environment(
                        args, execution_root, store, capabilities, session_resources
                    )
                    environment.workspace = workspace
                    with open_runtime(args, execution_root, store, environment, web) as session:
                        # Do not checkpoint a target that failed restoration or validation.
                        session.conversation.checkpoint(strict=True)
                        fallback_id = None
                        with ExitStack() as interaction:
                            interaction.callback(save_before_close, session.conversation)
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
                            interaction.callback(models.close)
                            options = {}
                            if environment.sandbox is not None:
                                options.update(
                                    sandbox=environment.sandbox, writeback=args.sandbox_writeback
                                )
                            result = run_interactive(
                                session.runtime,
                                thinking=session.thinking,
                                status=session.status,
                                models=models,
                                display=session.display,
                                conversation=session.conversation,
                                **options,
                            )
                            if isinstance(result, SessionSwitch):
                                next_args = continuation_args(args, session)
                            else:
                                return True
                # Teardown failures abort rather than starting a second uncertain backend.
                fallback_id, selector = store.id, result.session_id
                args = next_args
            except (LLMError, ValueError, OSError, subprocess.SubprocessError) as error:
                if fallback_id is None:
                    raise
                print(f"会话切换失败：{error}；正在恢复原会话。")
                selector, fallback_id = fallback_id, None
