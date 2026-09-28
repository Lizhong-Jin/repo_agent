"""CLI composition contracts: routing order, resource lifetime and dependency direction."""

import ast
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agent.session import SessionStore
from cli import application, execution_environment, main, runtime_setup, startup
from cli.runtime_events import SessionEvents
from llm import LLMClient


@pytest.mark.parametrize(
    "argv,module,expected",
    [
        (["config", "show"], "cli.config_command", ["show"]),
        (["doctor", "--skip-docker"], "cli.doctor", ["--skip-docker"]),
        (["toolchains", "list"], "installer.toolchains", ["list"]),
        (["uninstall", "--help"], "installer.uninstall", ["--help"]),
        (["sessions", "list"], "cli.sessions_command", ["list"]),
        (
            ["--root", "project", "sessions", "list"],
            "cli.sessions_command",
            ["--root", "project", "list"],
        ),
    ],
)
def test_explicit_argv_routes_commands_before_model_configuration(
    monkeypatch, argv, module, expected
):
    seen = []
    monkeypatch.setattr(importlib.import_module(module), "main", lambda args: seen.append(args))
    monkeypatch.setattr(
        startup, "configured_environment", lambda *a: pytest.fail("must not read model config")
    )
    monkeypatch.setattr("sys.argv", ["repo-agent", "--invalid-unrelated-flag"])
    main.main(argv)
    assert seen == [expected]


@pytest.mark.parametrize(
    "failure",
    ["tools", "runtime", "interactive", "cancel", "checkpoint", "native_close", "web_close"],
)
def test_failure_releases_native_client_web_and_real_session_lock(tmp_path, monkeypatch, failure):
    closed = []
    clients = []
    traces = []
    trace_files = []
    triggered = []
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fail(stage):
        if failure == stage:
            triggered.append(stage)
            raise OSError(stage + " failure")

    def web_close():
        closed.append("web")
        fail("web_close")

    monkeypatch.setattr(
        application.WebBackend,
        "from_environment",
        lambda: SimpleNamespace(adapter=None, pages=None, close=web_close),
    )

    class Backend:
        healthy = True

        def tools(self):
            fail("tools")
            return []

        def execution_context(self):
            return {"gpu_access": {"enabled": False}}

        def close(self):
            closed.append("native")
            fail("native_close")

    monkeypatch.setattr(execution_environment, "NativeBackend", lambda *a, **kw: Backend())

    def client_factory(config):
        http = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("startup must not generate a model response")
            )
        )
        client = LLMClient(config, http_client=http)
        client._owned = True
        client.get_context_limit = lambda **kw: None
        clients.append(http)
        return client

    monkeypatch.setattr(runtime_setup, "LLMClient", client_factory)
    real_runtime = runtime_setup.AgentRuntime

    def runtime_factory(*args, **kwargs):
        fail("runtime")
        return real_runtime(*args, **kwargs)

    monkeypatch.setattr(runtime_setup, "AgentRuntime", runtime_factory)
    real_tracer = runtime_setup.Tracer
    real_enter = real_tracer.__enter__

    def enter_trace(tracer):
        result = real_enter(tracer)
        # Tracer clears its own list on exit; retain the actual opened handles.
        trace_files.extend(tracer._files)
        return result

    monkeypatch.setattr(real_tracer, "__enter__", enter_trace)

    def tracer_factory(*args, **kwargs):
        tracer = real_tracer(*args, **kwargs)
        traces.append(tracer)
        return tracer

    monkeypatch.setattr(runtime_setup, "Tracer", tracer_factory)

    def interactive(runtime, **kwargs):
        fail("interactive")
        if failure == "cancel":
            triggered.append("cancel")
            raise KeyboardInterrupt
        if failure == "checkpoint":

            def checkpoint(**kw):
                triggered.append("checkpoint")
                return False

            kwargs["conversation"].checkpoint = checkpoint

    monkeypatch.setattr(application, "run_interactive", interactive)
    error_type = KeyboardInterrupt if failure == "cancel" else SystemExit
    with pytest.raises(error_type) as error:
        main.main(["--root", str(tmp_path), "--model", "mock", "--sandbox", "native"])
    if error_type is SystemExit:
        assert error.value.code == 1
    assert triggered == [failure], "CLI did not reach the intended failure stage"
    assert closed == ["native", "web"]
    expected_resources = 0 if failure == "tools" else 1
    assert len(clients) == len(traces) == expected_resources
    assert len(trace_files) == 4 * expected_resources  # Run and session logs, text and JSONL.
    assert all(http.is_closed for http in clients)
    assert all(file.closed for file in trace_files)
    # Reopening the actual project store proves that failure did not strand its lock.
    store = SessionStore(tmp_path).open()
    store.close()


def test_session_events_keep_accounting_and_tracing_when_notices_are_hidden():
    calls = []
    stats = SimpleNamespace(recoveries=[{"message": "retry"}])
    for visible in (False, True):
        calls.clear()
        events = SessionEvents(
            lambda *args: calls.append("status"),
            lambda *args: calls.append("trace"),
            show_notices=visible,
            emit=lambda text, **kw: calls.append(text),
        )
        events("recovery", stats)
        assert calls == (["status", "trace", "[retry]"] if visible else ["status", "trace"])


def test_cli_implementation_never_imports_composition_entrypoint():
    root = Path(__file__).resolve().parents[1]
    violations = []
    for path in (root / "cli").rglob("*.py"):
        if path == root / "cli/main.py":
            continue
        package = ".".join(path.relative_to(root).parts[:-1])
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                name = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                modules = [name, *(name + "." + alias.name for alias in node.names)]
            else:
                continue
            if "cli.main" in modules:
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "Reverse dependencies on the entrypoint: " + ", ".join(violations)
