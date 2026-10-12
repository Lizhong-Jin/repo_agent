"""Run with the installed Python -I: only the model is scripted, all tools are real."""

import argparse
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.report_reviews import record_review
from agent.session import SessionStore
from agent.task_reports import load_report
from agent.workspaces import Workspace, prepare_workspace
from cli.session_status import SessionStatus
from cli.task_controller import TaskController
from host_support.git_worktree import Git
from llm import LLMConfig, LLMResponse, Message, ToolCall
from sandbox.native import create_native_backend
from tools.factory import create_default_tools


class ScriptedModel:
    config = LLMConfig("deepseek", "acceptance", api_key="never-sent")

    def __init__(self):
        self.responses = iter(())

    def generate(self, request):
        return next(self.responses)

    def script(self, calls):
        self.responses = iter(
            [
                LLMResponse(
                    provider="deepseek",
                    model="acceptance",
                    message=Message("assistant", "", tool_calls=(call,)),
                    finish_reason="tool_calls",
                )
                for call in calls
            ]
            + [
                LLMResponse(
                    provider="deepseek",
                    model="acceptance",
                    message=Message("assistant", "done"),
                    finish_reason="stop",
                )
            ]
        )


def must_reject(operation):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError("Required delivery rejection did not occur")


def exercise(root, phase, mode):
    import agent

    assert Path(agent.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), agent.__file__
    if phase == "prepare":
        root.mkdir(parents=True)
        (root / "answer.txt").write_text("original")
        git = Git(root)
        git.run("init", "--template=", "--initial-branch=main")
        git.run("add", "answer.txt")
        git.run("commit", "-m", "acceptance baseline")
    with ExitStack() as stack:
        store = SessionStore(root).open()
        stack.callback(store.close)
        workspace = prepare_workspace(store, "worktree") if phase == "prepare" else Workspace(store)
        backend = None
        if mode == "native":
            backend = create_native_backend(
                workspace.root, profile="standard", isolated_workspace=True
            )
            stack.callback(backend.close)
        tools = backend.tools() if backend else create_default_tools(workspace.root)
        model = ScriptedModel()
        status = SessionStatus(root, context_window=32000)
        runtime = AgentRuntime(model, tools, on_event=status)
        argv = [
            sys.executable,
            "-I",
            "-c",
            "from pathlib import Path; assert Path('answer.txt').read_text() == 'correct'",
        ]
        checks = [
            {"id": "review", "title": "Review", "kind": "manual", "expectation": "Review changes"}
        ]
        if mode == "native":
            checks.insert(
                0,
                {
                    "id": "command",
                    "title": "Behavior",
                    "kind": "command",
                    "command": argv,
                    "cwd": ".",
                    "expectation": "Correct content, exit code zero",
                },
            )
        conversation = SavedConversation(
            store,
            runtime,
            model.config,
            status,
            workspace=workspace,
            execution_mode=mode,
            execution_backend=backend,
            delivery_policy={"version": 1, "mode": "strict", "checks": checks},
        )
        conversation.checkpoint(strict=True)
        controller = TaskController(
            runtime, conversation=conversation, write=print, write_model=print
        )
        if phase == "resume":
            assert controller.delivery_result()["state"] == "passed", controller.delivery_result()
            assert load_report(store, conversation.ledger)["delivery_record"]["state"] == "passed"
            assert len(controller.queue.pending) == 1
            controller.resume()
            assert not controller.queue.paused
            workspace.merge(workspace.review()["token"])
            assert (root / "answer.txt").read_text() == "correct"
            return
        controller.enqueue("Create candidate")
        controller.enqueue("Must remain queued until accepted")

        def attempt(content):
            calls = [
                ToolCall(
                    "write-" + content,
                    "write_file",
                    {"path": "answer.txt", "content": content, "overwrite": True},
                )
            ]
            if mode == "native":
                calls.append(
                    ToolCall("check-" + content, "run_command", {"command": argv, "cwd": "."})
                )
            model.script(calls)
            task = controller.start_next()
            assert task is not None
            outcome = controller.execute(task)
            assert controller.finish(task, outcome) == "completed", outcome
            assert (workspace.root / "answer.txt").read_text() == content
            return task

        attempt("wrong")
        assert controller.delivery_result()["state"] == "blocked", controller.delivery_result()
        must_reject(controller.resume)
        must_reject(lambda: workspace.merge(workspace.review()["token"]))
        assert (root / "answer.txt").read_text() == "original"
        controller.command("/delivery repair Fix content and recheck")
        task = attempt("correct")
        assert controller.delivery_result()["state"] == "blocked", controller.delivery_result()
        report = load_report(store, conversation.ledger)
        if mode == "native":
            command = next(item for item in report["verification_items"] if item["id"] == "command")
            assert command["state"] == "passed", command
        record_review(
            store, str(task["number"]), "review", "accepted", "Checked installed workflow"
        )
        controller.command("/delivery refresh")
        assert controller.delivery_result()["state"] == "passed", controller.delivery_result()
        assert controller.queue.paused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=("prepare", "resume"), required=True)
    args = parser.parse_args()
    exercise(args.root.resolve(), args.phase, "local" if os.name == "nt" else "native")
    print(json.dumps({"phase": args.phase, "status": "passed", "python": sys.executable}))


if __name__ == "__main__":
    main()
