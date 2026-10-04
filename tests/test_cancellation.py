"""Cancellation crosses I/O boundaries and waits for owned work to settle."""

import asyncio
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from agent import AgentRuntime
from cli.cancellation import cancellation_notice
from cli.task_execution import TaskRunner
from host_support.cancellation import (
    CancellationContext,
    RunCancelled,
    cancellation_scope,
    checkpoint,
    current_cancellation,
)
from host_support.processes import ProcessRunner
from llm import AsyncLLMClient, LLMClient, LLMConfig, LLMRequest, Message, ToolDefinition
from tools import ExecutionKind, ToolDispatcher, ToolResult

REQUEST = LLMRequest([Message("user", "hello")])
PAYLOAD = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


def test_context_is_per_run_and_restored_after_failure():
    with cancellation_scope() as first:
        with cancellation_scope() as nested:
            assert nested is first
        first.cancel()
        with pytest.raises(RunCancelled):
            checkpoint()
    assert current_cancellation() is None
    with cancellation_scope() as second:
        assert second is not first
        checkpoint()


def test_sigint_requests_stop_without_interrupting_atomic_section():
    previous = signal.getsignal(signal.SIGINT)
    with cancellation_scope(handle_sigint=True) as context:
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        assert context.event.is_set()
        with pytest.raises(RunCancelled):
            checkpoint()
    assert signal.getsignal(signal.SIGINT) == previous


@pytest.mark.parametrize("phase", ["headers", "stream", "body", "retry"])
def test_default_model_transport_cancels_and_next_request_works(phase, monkeypatch):
    started = threading.Event()
    closed = threading.Event()
    requests = []
    original_async_client = httpx.AsyncClient

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.sleep(60)
            yield b"unreachable"

        async def aclose(self):
            closed.set()

    async def handler(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, json=PAYLOAD)
        if phase == "headers":
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                closed.set()
        if phase == "retry":
            started.set()
            return httpx.Response(429, headers={"retry-after": "30"})
        media_type = "text/event-stream" if phase == "stream" else "application/json"
        return httpx.Response(200, headers={"content-type": media_type}, stream=StalledStream())

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: original_async_client(transport=httpx.MockTransport(handler))
    )
    context = CancellationContext()

    def stop():
        assert started.wait(3)
        context.cancel()

    thread = threading.Thread(target=stop)
    thread.start()
    with LLMClient(LLMConfig("deepseek", "test", api_key="synthetic")) as client:
        with cancellation_scope(context), pytest.raises(RunCancelled):
            client.generate(REQUEST)
        assert len(requests) == 1
        if phase != "retry":
            assert closed.is_set()
        bridge = client._async_bridge
        with cancellation_scope():
            assert client.generate(REQUEST).text == "ok"
        assert len(requests) == 2
    thread.join(timeout=3)
    assert not thread.is_alive() and not bridge.thread.is_alive()


def test_sync_injected_transport_retry_wait_is_interruptible():
    context = CancellationContext()
    requests = []

    def handler(request):
        requests.append(request)
        context.cancel()
        return httpx.Response(503, headers={"retry-after": "30"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = LLMClient(LLMConfig("deepseek", "test", api_key="synthetic"), http_client=http)
        start = time.monotonic()
        with cancellation_scope(context), pytest.raises(RunCancelled):
            client.generate(REQUEST)
        assert time.monotonic() - start < 1
        assert len(requests) == 1 and not http.is_closed


def test_async_request_pre_cancelled_never_sends():
    async def run():
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=PAYLOAD)

        context = CancellationContext()
        context.cancel()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = AsyncLLMClient(LLMConfig("deepseek", "m", api_key="x"), http_client=http)
            with cancellation_scope(context), pytest.raises(RunCancelled):
                await client.generate(REQUEST)
        assert not requests

    asyncio.run(run())


def test_atomic_tool_finishes_and_records_result_before_cancellation(tmp_path):
    context = CancellationContext()
    target = tmp_path / "result.txt"

    class Write:
        execution_kind = ExecutionKind.TRUSTED_FILE
        definition = ToolDefinition("write_file", "test")

        def execute(self, arguments):
            context.cancel()
            checkpoint()  # A file transaction defers even nested cancellation checks.
            target.write_text("complete")
            return ToolResult(True, {"path": "result.txt", "created": True}).with_effects()

    dispatcher = ToolDispatcher()
    dispatcher.register(Write())
    with cancellation_scope(context), pytest.raises(RunCancelled) as error:
        dispatcher.execute("write_file", {})
    assert target.read_text() == "complete"
    assert error.value.report["tools"][0]["status"] == "completed"
    assert error.value.report["tools"][0]["result"]["path"] == "result.txt"
    # A second call is rejected before touching the file.
    target.write_text("keep")
    with cancellation_scope(context), pytest.raises(RunCancelled):
        dispatcher.execute("write_file", {})
    assert target.read_text() == "keep"


def test_cancel_process_waits_for_exit_and_keeps_cleanup_result(tmp_path, monkeypatch):
    import host_support.processes as processes

    context = CancellationContext()
    spawned = []
    original_start = processes.start_process
    timer = None

    def start(*args, **kwargs):
        nonlocal timer
        process = original_start(*args, **kwargs)
        spawned.append(process)
        timer = threading.Timer(0.1, context.cancel)
        timer.start()
        return process

    monkeypatch.setattr(processes, "start_process", start)
    before = time.monotonic()
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled) as error:
            ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
        assert time.monotonic() - before < 5
        assert spawned[0].poll() is not None
        report = error.value.report
        assert report["cleanup_status"] == ("confirmed" if os.name == "posix" else "unknown")
        assert report["cleanups"][0]["pid"] == spawned[0].pid
    finally:
        if timer is not None:
            timer.join()
        for process in spawned:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_pre_cancelled_process_never_starts(tmp_path, monkeypatch):
    import host_support.processes as processes

    def forbidden(*args, **kwargs):
        pytest.fail("cancelled run spawned a process")

    monkeypatch.setattr(processes, "start_process", forbidden)
    context = CancellationContext()
    context.cancel()
    with pytest.raises(RunCancelled):
        ProcessRunner().run([sys.executable], cwd=tmp_path, cancellation=context)


def test_cancelled_task_is_not_error_and_does_not_write_back():
    context = CancellationContext()

    def run(*args, **kwargs):
        context.record_cleanup("unknown", pid=123, error="unconfirmed descendants")
        context.cancel()
        checkpoint()

    output = []
    runner = TaskRunner(
        SimpleNamespace(run=run), checkpoint, output.append, output.append, cancellation=context
    )
    kind, outcome = runner.run("task")
    assert kind == "cancelled" and outcome.report["cleanup_status"] == "unknown"
    assert "清理尚未确认" in cancellation_notice(outcome.report) and not output


def test_runtime_records_cancellation_and_stops_before_next_model_call():
    context = CancellationContext()

    class Model:
        def generate(self, request):
            context.cancel()
            checkpoint()

    runtime = AgentRuntime(Model())
    with pytest.raises(RunCancelled):
        runtime.run("task", cancellation=context)
    assert runtime.last_stats.status == "cancelled"
    assert runtime.last_stats.cancellation["status"] == "cancelled"


@pytest.mark.parametrize("operation", ["search", "fetch"])
def test_web_wait_is_cancelled_after_request_finalizer(operation):
    from tools._internal.web_backend import WebBackend

    context = CancellationContext()
    started = threading.Event()
    settled = threading.Event()

    class Backend:
        name = "test"
        cache = {}

        async def search(self, *args):
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                settled.set()

        async def retrieve(self, *args, **kwargs):
            return await self.search()

    backend = WebBackend(Backend(), fetch_enabled=True, pages=Backend())

    def stop():
        assert started.wait(3)
        context.cancel()

    thread = threading.Thread(target=stop)
    thread.start()
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled):
            if operation == "search":
                backend.search(["query"], [], 1)
            else:
                backend.fetch([{"url": "https://example.com"}])
        assert settled.is_set()
    finally:
        backend.close()
        thread.join(timeout=3)
    assert not thread.is_alive() and not backend._thread.is_alive()


def test_lsp_initialization_wait_cancels_and_reaps_server(tmp_path):
    from pathlib import Path

    from tools._internal.lsp_client import LspClient

    server = Path(__file__).parent / "fixtures" / "lsp_server.py"
    context = CancellationContext()
    connection = LspClient(
        tmp_path, [sys.executable, str(server), "hang"], language_id="python", timeout_seconds=60
    )
    timer = threading.Timer(0.2, context.cancel)
    timer.start()
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled) as error:
            connection.start()
        assert connection._process.poll() is not None
        assert all(not t.is_alive() for t in connection._threads)
        assert error.value.report["cleanup_status"] == "unknown"
    finally:
        timer.join()
        connection.close()


def test_cleanup_failure_is_not_misreported_as_success(tmp_path, monkeypatch):
    import host_support.processes as processes

    context = CancellationContext()
    spawned = []
    original_start = processes.start_process

    def start(*args, **kwargs):
        process = original_start(*args, **kwargs)
        spawned.append(process)
        context.cancel()
        return process

    def failed_cleanup(*args):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(processes, "start_process", start)
    monkeypatch.setattr(ProcessRunner, "_terminate_process_tree", failed_cleanup)
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled) as error:
            ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
        assert error.value.report["cleanup_status"] == "unknown"
    finally:
        for process in spawned:
            process.kill()
            process.wait()


def test_cancellation_report_persisted_in_conversation(open_conversation):
    context = CancellationContext()
    context.record_cleanup("unknown", pid=123)
    conversation = open_conversation()
    conversation.start_task("stop me")
    conversation.fail_task(cancellation=context.report())
    text = conversation.store.catalog.log_path(conversation.store.id).read_text()
    assert "用户主动停止" in text and '"cleanup_status": "unknown"' in text
    assert conversation.pending_task is None


@pytest.mark.parametrize("cleanup_unconfirmed", [False, True])
def test_tui_cancel_stops_process_and_allows_fresh_task(tmp_path, monkeypatch, cleanup_unconfirmed):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from cli.terminal.application import ConversationUI
    from llm import LLMResponse, ToolCall

    started = threading.Event()

    import host_support.processes as processes

    original_start = processes.start_process

    def start(*args, **kwargs):
        process = original_start(*args, **kwargs)
        started.set()
        return process

    monkeypatch.setattr(processes, "start_process", start)

    if cleanup_unconfirmed:
        original_cleanup = ProcessRunner._terminate_process_tree

        def unconfirmed_cleanup(process, diagnostics=None):
            # Terminate the real child, then exercise Windows-style uncertainty on every OS.
            original_cleanup(process, diagnostics)
            return "Descendant process termination is not guaranteed on this platform."

        monkeypatch.setattr(
            ProcessRunner, "_terminate_process_tree", staticmethod(unconfirmed_cleanup)
        )

    class Command:
        # Trusted test double; production process tools still require sandbox adapters.
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = ToolDefinition("test_command", "test")

        def execute(self, arguments):
            ProcessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path)
            pytest.fail("cancelled process returned normally")

    class Model:
        calls = 0

        def generate(self, request):
            self.calls += 1
            message = (
                Message("assistant", tool_calls=[ToolCall("call", "test_command", {})])
                if self.calls == 1
                else Message("assistant", "fresh task complete")
            )
            return LLMResponse(
                "deepseek", "m", message, finish_reason="tool_calls" if self.calls == 1 else "stop"
            )

    async def until(predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 5)

    async def run():
        runtime = AgentRuntime(Model(), [Command()])
        with create_pipe_input() as pipe:
            ui = ConversationUI(runtime, terminal_input=pipe, terminal_output=DummyOutput())
            task = asyncio.create_task(ui.run_async())
            try:
                await until(lambda: ui.app.is_running)
                pipe.send_text("first\r")
                await until(started.is_set)
                first = ui.cancellation
                pipe.send_text("\x03\x03")
                await until(lambda: not ui.busy)
                assert "用户主动停止" in ui.transcript
                assert runtime.last_stats.status == "cancelled"
                assert first.report()["cleanup_status"] in {"confirmed", "unknown"}
                if cleanup_unconfirmed:
                    assert first.report()["cleanup_status"] == "unknown"
                    assert "进程清理未确认；队列已暂停" in ui.transcript
                pipe.send_text("second\r")
                await until(lambda: len(ui.controller.queue.pending) == 1)
                assert ui.controller.queue.paused and runtime.llm.calls == 1
                pipe.send_text("/queue resume\r")
                if first.report()["cleanup_status"] == "unknown":
                    await until(lambda: "进程清理尚未确认" in ui.phase)
                    assert runtime.llm.calls == 1
                    assert ui.controller.cleanup_blocked and ui.controller.queue.paused
                    # Rejected commands remain editable; Ctrl+D exits only an empty editor.
                    assert ui.editor.text == "/queue resume"
                    pipe.send_text("\x03\x04")
                    await asyncio.wait_for(task, 5)
                    return
                await until(lambda: "fresh task complete" in ui.transcript)
                await until(lambda: not ui.busy)
                assert ui.cancellation is not first and not ui.cancelled.is_set()
                pipe.send_text("\x04")
                await asyncio.wait_for(task, 5)
            finally:
                if not task.done():
                    ui.cancelled.set()
                    ui.app.exit()
                    await asyncio.wait_for(task, 5)

    asyncio.run(run())


@pytest.mark.parametrize("cleanup_ok", [True, False])
def test_docker_cancellation_waits_for_container_removal(tmp_path, monkeypatch, cleanup_ok):
    import subprocess

    from sandbox.docker import DockerBackend
    from sandbox.policy import SandboxPolicy

    context = CancellationContext()
    removals = []
    backend = object.__new__(DockerBackend)
    backend.policy = SandboxPolicy()
    backend.image = "test-image"
    backend.executable = "docker"
    backend.healthy = True

    def execute(*args, **kwargs):
        context.cancel()
        checkpoint()

    def remove(command, **kwargs):
        removals.append(command)
        context.cancel()  # Repeated cancellation cannot bypass container cleanup.
        if not cleanup_ok:
            raise subprocess.TimeoutExpired(command, 15)
        return SimpleNamespace(returncode=0)

    backend.runner = SimpleNamespace(run=execute)
    monkeypatch.setattr("sandbox.docker.subprocess.run", remove)
    with cancellation_scope(context), pytest.raises(RunCancelled) as error:
        backend.execute(tmp_path, "run_command", {"command": ["test"]})
    assert len(removals) == 1 and removals[0][:3] == ["docker", "rm", "-f"]
    assert backend.healthy is cleanup_ok
    assert error.value.report["cleanup_status"] == ("confirmed" if cleanup_ok else "unknown")


def test_model_dns_cancellation_does_not_wait_for_resolver(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    connected = []
    context = CancellationContext()
    original = httpx.AsyncClient

    def resolve():
        started.set()
        try:
            release.wait(5)
        finally:
            finished.set()

    async def handler(request):
        await asyncio.get_running_loop().run_in_executor(None, resolve)
        connected.append(True)
        return httpx.Response(200, json=PAYLOAD)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: original(transport=httpx.MockTransport(handler))
    )

    def stop():
        assert started.wait(3)
        context.cancel()

    thread = threading.Thread(target=stop)
    thread.start()
    try:
        start = time.monotonic()
        with LLMClient(LLMConfig("deepseek", "m", api_key="x")) as client:
            with cancellation_scope(context), pytest.raises(RunCancelled):
                client.generate(REQUEST)
        assert time.monotonic() - start < 2
        assert not finished.is_set()
    finally:
        release.set()
        thread.join(timeout=3)
        assert finished.wait(3)
    assert not connected


def test_default_model_legacy_callback_interrupt_keeps_io_loop_usable(monkeypatch):
    original = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(200, json=PAYLOAD)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda: original(transport=httpx.MockTransport(handler))
    )

    def interrupt(kind, *args):
        if kind == "end":
            raise KeyboardInterrupt

    with LLMClient(LLMConfig("deepseek", "m", api_key="x")) as client:
        with cancellation_scope(), pytest.raises(RunCancelled):
            client.generate_with_events(REQUEST, interrupt)
        with cancellation_scope():
            assert client.generate(REQUEST).text == "ok"
