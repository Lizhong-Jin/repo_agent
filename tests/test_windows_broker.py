"""The host broker owns policy but never executes probes outside isolation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from host_support.processes import ProcessResult, ProcessStartError
from sandbox.windows_broker import WindowsEnvironmentService, _EnvironmentRunner
from tools.factory import create_default_tools


def backend_stub(tmp_path):
    python = tmp_path / "python" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"MZ")
    runtime = tmp_path / "trusted"
    (runtime / "sandbox").mkdir(parents=True)
    (runtime / "sandbox" / "compute_probe.py").write_text("print('trusted probe')")
    return SimpleNamespace(
        workspace=tmp_path,
        healthy=True,
        runtime=runtime,
        _project_layout=SimpleNamespace(executable=python, base=python.parent),
        project_python=SimpleNamespace(executable=python),
        system_directory=tmp_path / "System32",
        _tool_limits=lambda: {},
        execution_context=lambda: {
            "mode": "native",
            "platform": "windows",
            "network": "disabled",
            "writeback_mode": "per_call_after_cleanup",
            "python_environments": {
                "project": str(python),
                "project_info": {"version": "3.12"},
            },
        },
    )


def test_execution_policy_report_does_not_start_a_worker(tmp_path):
    backend = backend_stub(tmp_path)
    backend._run = lambda *a, **kw: pytest.fail("Static report launched a process")
    result = WindowsEnvironmentService(backend).execute({"sections": ["execution"]})
    assert result.success
    assert result.data["execution"]["network"] == "disabled"
    assert result.data["execution"]["working_directory"] == "per-call private workspace"
    assert result.data["execution"]["writeback_mode"] == "per_call_after_cleanup"


def test_invalid_sections_do_not_start_a_probe(tmp_path):
    backend = backend_stub(tmp_path)
    backend._run = lambda *a, **kw: pytest.fail("Invalid request launched a process")
    result = WindowsEnvironmentService(backend).execute({"sections": ["run_command"]})
    assert not result.success and result.error_code == "INVALID_ARGUMENTS"


def test_probe_uses_frozen_source_and_isolated_runner(tmp_path):
    backend = backend_stub(tmp_path)
    calls = []
    backend._run = lambda command, **kwargs: calls.append((command, kwargs))
    from sandbox import windows_broker

    script = Path(windows_broker.__file__).with_name("compute_probe.py")
    _EnvironmentRunner(backend).run(
        [str(backend.project_python.executable), "-I", str(script)],
        cwd=tmp_path,
        timeout_seconds=3,
    )
    command, options = calls[0]
    assert command[-2:] == ["-c", "print('trusted probe')"]
    assert options["project"] and options["cwd"] == tmp_path
    assert options["timeout"] == 3 and options["max_output_bytes"] == 32768


@pytest.mark.parametrize("variant", ["arbitrary", "outside", "unhealthy"])
def test_broker_rejects_commands_or_context_outside_contract(tmp_path, variant):
    backend = backend_stub(tmp_path)
    backend._run = lambda *a, **kw: pytest.fail("Rejected probe reached executor")
    command = ["node.exe", "--version"]
    cwd = tmp_path
    if variant == "arbitrary":
        command = [str(backend.project_python.executable), "-c", "print('untrusted')"]
    elif variant == "outside":
        cwd = tmp_path.parent
    else:
        backend.healthy = False
    with pytest.raises(ProcessStartError):
        _EnvironmentRunner(backend).run(command, cwd=cwd, timeout_seconds=3)


def test_runtime_versions_come_from_isolation_not_ambient_path(tmp_path, monkeypatch):
    backend = backend_stub(tmp_path)
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return ProcessResult(
            exit_code=0,
            stdout="isolated-version\n",
            stderr="",
            timed_out=False,
            cleanup_error=None,
            duration_ms=1,
            stdout_truncated=False,
            stderr_truncated=False,
            cleanup_status="confirmed",
        )

    backend._run = run

    (backend._project_layout.base / "node.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PATH", "HOST_SECRET_PATH")
    result = WindowsEnvironmentService(backend).execute({"sections": ["runtimes"]})
    assert result.success and result.data["runtimes"]["node"]["version"] == "isolated-version"
    assert len(calls) == 1 and calls[0][1]["project"]


def test_single_tool_does_not_construct_unrelated_tools(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unrelated tool constructed")

    monkeypatch.setattr("tools.factory.GetExecutionEnvironmentTool", forbidden)
    monkeypatch.setattr("tools.factory.ReadFileTool", forbidden)
    monkeypatch.setattr("tools.factory.GitDiffTool", forbidden)
    tools = create_default_tools(tmp_path, isolated_execution=True, tool_name="get_symbols")
    assert [tool.definition.name for tool in tools] == ["get_symbols"]
    assert create_default_tools(tmp_path, isolated_execution=False, tool_name="get_symbols") == []
    assert create_default_tools(tmp_path, isolated_execution=True, tool_name="unknown") == []


def test_unknown_cleanup_stops_remaining_probes(tmp_path, monkeypatch):
    backend = backend_stub(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        backend.healthy = False
        return ProcessResult(
            exit_code=0,
            stdout="version",
            stderr="",
            timed_out=False,
            cleanup_error=None,
            duration_ms=1,
            stdout_truncated=False,
            stderr_truncated=False,
            cleanup_status="unknown",
        )

    backend._run = run
    monkeypatch.setattr(
        WindowsEnvironmentService, "_find_executable", lambda self, name: name + ".exe"
    )
    result = WindowsEnvironmentService(backend).execute({"sections": ["runtimes"]})
    assert not result.success and result.error_code == "PROBE_CLEANUP_FAILED"
    assert result.data["execution_allowed"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("isolated", [False, True])
def test_selecting_each_tool_preserves_catalog_definition(tmp_path, isolated):
    catalog = create_default_tools(tmp_path, isolated_execution=isolated)
    for expected in catalog:
        selected = create_default_tools(
            tmp_path,
            isolated_execution=isolated,
            tool_name=expected.definition.name,
        )
        assert len(selected) == 1
        assert selected[0].definition == expected.definition
        assert type(selected[0]) is type(expected)


def test_backend_rejects_environment_request_for_another_workspace(tmp_path):
    from sandbox.windows_native import WindowsNativeBackend

    backend = object.__new__(WindowsNativeBackend)
    backend.workspace = tmp_path
    backend.healthy = True
    with pytest.raises(ValueError, match="不能切换工作区"):
        backend._execute(tmp_path.parent, "get_execution_environment", {})


def test_probe_discovery_ignores_cwd_and_ambient_extensions(tmp_path, monkeypatch):
    backend = backend_stub(tmp_path)
    (tmp_path / "node.exe").write_bytes(b"MZ untrusted cwd")
    (backend._project_layout.base / "node.cmd").write_text("echo untrusted shell")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")
    service = WindowsEnvironmentService(backend)
    assert service._find_executable("node") is None
