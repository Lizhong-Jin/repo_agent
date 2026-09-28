"""Contracts between headless session services, runtime events and terminal UI."""

import ast
import asyncio
import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent import AgentRuntime
from agent.thinking import ThinkingController
from agent.Tracing import ModelCallRecord, RunStats
from cli.runtime_events import RuntimeEventBridge
from cli.task_execution import TaskRunner
from cli.terminal.application import ConversationUI
from llm import Usage

ROOT = Path(__file__).resolve().parents[1]


def test_core_session_services_work_with_cli_and_terminal_imports_blocked(tmp_path):
    script = """
import importlib.abc
import sys

class NoPresentation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cli', 'prompt_toolkit', 'configuration'}:
            raise AssertionError('Core service imported presentation/storage: ' + fullname)

sys.meta_path.insert(0, NoPresentation())
from agent import AgentRuntime
from agent.session_state import SessionState
from agent.thinking import ThinkingController
from agent.Tracing import ModelCallRecord, RunStats
from llm import Usage

state = SessionState('.')
stats = RunStats(1)
stats.model_calls.append(ModelCallRecord(1, usage=Usage(12, 3)))
state('model_end', stats)
state('model_end', stats)
restored = SessionState('.')
restored.restore_session(state.session_state())
assert restored.calls == 1 and restored.context_tokens == 15
runtime = AgentRuntime(object())
control = ThinkingController(runtime, provider='zhipu', model='glm-5.2', max_output_tokens=4096)
control.set('disabled')
assert runtime.request_extra['thinking'] == {'type': 'disabled'}
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_persistence_port_failure_keeps_runtime_and_controller_unchanged():
    saved = []

    class Preferences:
        def load(self, provider, model, base_url):
            return None

        def save(self, *args, **kwargs):
            saved.append(deepcopy((args, kwargs)))
            if len(saved) > 1:
                raise OSError("disk full")

    runtime = AgentRuntime(object(), request_extra={"top_p": 0.8})
    control = ThinkingController(
        runtime,
        provider="zhipu",
        model="glm-5.2",
        max_output_tokens=4096,
        extra={"top_p": 0.8},
        preferences=Preferences(),
    )
    control.set("disabled")
    before = deepcopy((control.current, runtime.request_extra, runtime.thinking_settings))
    with pytest.raises(OSError, match="disk full"):
        control.set("enabled", "high")
    assert (control.current, runtime.request_extra, runtime.thinking_settings) == before
    assert saved[0][0][3]["mode"] == "disabled"


def test_event_bridge_snapshots_and_restores_callbacks_without_a_terminal():
    pending, delivered, live_events = [], [], []
    status = {"text": "before"}

    def original_event(name, stats):
        status["text"] = f"usage {stats.model_calls[-1].usage.input_tokens}"

    original_model, original_cancel = object(), object()
    runtime = SimpleNamespace(
        on_event=original_event,
        on_model_event=original_model,
        check_cancelled=original_cancel,
    )
    stats = RunStats(1)
    record = ModelCallRecord(1, usage=Usage(12, 3))
    stats.model_calls.append(record)

    def sink(*values):
        delivered.append(values)

    def live(*values):
        live_events.append(values)
        return 0.25

    bridge = RuntimeEventBridge(
        runtime,
        dispatch=lambda callback, *args: pending.append((callback, args)),
        progress=sink,
        model_boundary=sink,
        thinking=sink,
        status_text=lambda: status["text"],
        write_meta=sink,
        live=live,
        check_cancelled=lambda: None,
    )
    with pytest.raises(OSError, match="cleanup failed"), bridge:
        runtime.on_event("model_start", stats)
        runtime.on_model_event("thinking_delta", "thought", 0.1, record)
        assert runtime.on_model_event("text", "answer", 0.2, record) == 0.25
        status["text"] = "later state"
        record.step = 9
        # dispatch queues calls; no screen state was touched in the producer thread.
        assert delivered == []
        for callback, args in pending:
            callback(*args)
        assert ("model_start", 1) in delivered
        assert ("thinking_delta", "thought", 1, 0.1) in delivered
        assert any("usage 12" in values for values in delivered)
        assert all("later state" not in values for values in delivered)
        assert [event[0] for event in live_events] == ["text"]
        raise OSError("cleanup failed")
    assert (runtime.on_event, runtime.on_model_event, runtime.check_cancelled) == (
        original_event,
        original_model,
        original_cancel,
    )


def test_task_runner_cancellation_prevents_writeback_and_late_output():
    calls = []
    result = SimpleNamespace(undisplayed_text="answer", status="completed")
    runtime = SimpleNamespace(run=lambda task, history: result)
    sandbox = SimpleNamespace(begin_task=lambda: calls.append("begin"))

    def cancel():
        raise KeyboardInterrupt

    runner = TaskRunner(
        runtime,
        check_cancelled=cancel,
        write=calls.append,
        write_model=calls.append,
        sandbox=sandbox,
        writeback_mode="on-success",
    )
    kind, message = runner.run("task", history=("prior message",))
    assert kind == "error" and "中断" in message
    # Sandbox intentionally has no writeback API: cancellation must stop before it.
    assert calls == ["begin"]


@pytest.mark.parametrize("failure", ["app", "worker", "finished worker", "checkpoint"])
def test_ui_shutdown_always_restores_runtime_hooks(failure):
    async def run():
        runtime = AgentRuntime(object())
        previous = (runtime.on_event, runtime.on_model_event, runtime.check_cancelled)
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())

            async def worker():
                await asyncio.sleep(0)
                raise OSError(failure + " failure")

            async def app():
                if failure == "app":
                    raise OSError("app failure")
                if failure in {"worker", "finished worker"}:
                    ui.worker = asyncio.create_task(worker())
                if failure == "finished worker":
                    await asyncio.wait([ui.worker])

            def checkpoint(**kwargs):
                raise OSError("checkpoint failure")

            ui.app.run_async = app
            if failure == "checkpoint":
                ui.conversation = SimpleNamespace(checkpoint=checkpoint)
            with pytest.raises(OSError, match=failure + " failure"):
                await ui.run_async()
            assert ui.cancelled.is_set()
            assert ui.flush_handle is None
            assert (runtime.on_event, runtime.on_model_event, runtime.check_cancelled) == previous

    asyncio.run(run())


def test_checkpoint_failure_releases_busy_state():
    async def run():
        runtime = AgentRuntime(object())
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())

            def checkpoint(**kwargs):
                raise OSError("checkpoint failure")

            ui.conversation = SimpleNamespace(label="会话", checkpoint=checkpoint)
            ui.work = lambda task: ("command", None)
            ui.busy = True
            with pytest.raises(OSError, match="checkpoint failure"):
                await ui.execute("/diff")
            assert not ui.busy

    asyncio.run(run())


def terminal_dependency_violations(path, root):
    package = ".".join(path.relative_to(root).parts[:-1])
    forbidden = {"cli.live", "cli.tui", "cli.interactive", "cli.main"}
    if not path.is_relative_to(root / "cli/terminal"):
        forbidden.add("cli.terminal")
        if path.stem != "input":
            forbidden.add("prompt_toolkit")
    violations = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            name = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            modules = [name, *(name + "." + alias.name for alias in node.names)]
        else:
            continue
        for module in modules:
            if any(module == f or module.startswith(f + ".") for f in forbidden):
                violations.append(f"{path.relative_to(root)}:{node.lineno}: {module}")
    return violations


@pytest.mark.parametrize(
    "relative_path,source,forbidden",
    [
        ("cli/task_execution.py", "import cli.terminal", True),
        ("cli/task_execution.py", "from cli import terminal", True),
        ("cli/task_execution.py", "from . import terminal as screen", True),
        ("cli/task_execution.py", "from .terminal import application", True),
        ("cli/task_execution.py", "from cli import interactive as ui", True),
        ("cli/task_execution.py", "def run():\n    from . import main", True),
        ("cli/task_execution.py", "from prompt_toolkit import Application", True),
        ("cli/terminal/widgets.py", "from .. import interactive", True),
        ("cli/terminal/widgets.py", "from . import layout", False),
        ("cli/terminal/widgets.py", "from .. import task_execution", False),
        ("cli/input.py", "from prompt_toolkit import PromptSession", False),
        ("cli/task_execution.py", "from . import output", False),
    ],
)
def test_terminal_dependency_checker_handles_import_forms(
    tmp_path, relative_path, source, forbidden
):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    assert bool(terminal_dependency_violations(path, tmp_path)) is forbidden


def test_terminal_dependencies_flow_toward_services_and_never_back():
    services = [
        "input",
        "output",
        "session_status",
        "thinking_control",
        "task_execution",
        "runtime_events",
        "conversation_help",
        "writeback",
    ]
    paths = [*(ROOT / "cli/terminal").rglob("*.py")]
    paths += [ROOT / "cli" / f"{name}.py" for name in services]
    violations = [
        violation for path in paths for violation in terminal_dependency_violations(path, ROOT)
    ]
    assert not violations, "\n".join(violations)
