"""Every tool declares a route; declaring a process never grants host execution."""

import json
from types import SimpleNamespace

import pytest

from agent import AgentRuntime
from agent.history import HistoryTool
from agent.skills import LoadSkillTool, SkillRegistry
from llm import ToolCall, ToolDefinition
from sandbox.native import NativeTool
from sandbox.session import SandboxedTool
from sandbox.writeback import WritebackGuard
from tools import ExecutionKind, ToolDispatcher, ToolResult, create_default_tools
from tools._internal.base import execution_kind_of
from tools.execute import GetExecutionEnvironmentTool, RunCommandTool, RunPythonTool
from tools.git_tools import GitDiffTool, GitStatusTool
from tools.tool_groups import DEFAULT_TOOL_GROUPS, LoadToolGroupTool, ToolGroupRegistry
from tools.web_tools import WebFetchTool, WebSearchTool


class HostTool:
    execution_kind = ExecutionKind.HOST_CONTROL
    definition = ToolDefinition("example", "Host test operation")

    def execute(self, arguments):
        return ToolResult(True, {"arguments": arguments})


@pytest.mark.parametrize("kind", [None, "host_control", "trusted_file", False, 1, object()])
def test_invalid_or_missing_rule_rejected_before_model_request(kind):
    attrs = {"definition": HostTool.definition, "execute": HostTool.execute}
    if kind is not None:
        attrs["execution_kind"] = kind
    tool = type("UndeclaredTool", (), attrs)()
    with pytest.raises(ValueError, match="UndeclaredTool.*explicitly declare execution_kind"):
        AgentRuntime(object(), [tool])


def test_new_subclass_must_explicitly_reconsider_its_route():
    class ImplicitTool(HostTool):
        pass

    with pytest.raises(ValueError, match="explicitly declare"):
        ToolDispatcher().register(ImplicitTool())

    class ExplicitTool(HostTool):
        execution_kind = ExecutionKind.HOST_CONTROL

    dispatcher = ToolDispatcher()
    dispatcher.register(ExplicitTool())
    assert dispatcher.execute("example", {}).success


def test_slotted_tool_can_declare_class_metadata():
    class SlottedTool:
        __slots__ = ()
        execution_kind = ExecutionKind.HOST_CONTROL
        definition = HostTool.definition
        execute = HostTool.execute

    assert execution_kind_of(SlottedTool()) is ExecutionKind.HOST_CONTROL


def test_factories_validate_new_tools_even_without_runtime(tmp_path, monkeypatch):
    from tools import factory

    class UndeclaredTool:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(factory, "ReadFileTool", UndeclaredTool)
    with pytest.raises(ValueError, match="execution_kind"):
        factory.create_file_tools(tmp_path)
    with pytest.raises(ValueError, match="execution_kind"):
        factory.create_default_tools(tmp_path)


def test_every_builtin_tool_has_concrete_metadata_and_unchanged_model_schema(tmp_path):
    defaults = create_default_tools(tmp_path, isolated_execution=True)
    controls = [
        LoadSkillTool(SkillRegistry(tmp_path)),
        LoadToolGroupTool(ToolGroupRegistry(DEFAULT_TOOL_GROUPS, [])),
        HistoryTool(None, "search"), HistoryTool(None, "read"),
    ]
    network = [WebSearchTool(None), WebFetchTool(None)]
    for tool in [*defaults, *controls, *network]:
        assert isinstance(vars(type(tool))["execution_kind"], ExecutionKind)
        assert execution_kind_of(tool) is tool.execution_kind
        assert "execution_kind" not in tool.definition.parameters.get("properties", {})
    assert all(t.execution_kind is ExecutionKind.HOST_CONTROL for t in controls)
    assert all(t.execution_kind is ExecutionKind.TRUSTED_NETWORK for t in network)
    kinds = {t.definition.name: t.execution_kind for t in defaults}
    assert {name for name, kind in kinds.items() if kind is ExecutionKind.SANDBOXED_PROCESS} == {
        "get_execution_environment", "run_command", "run_python", "git_status", "git_diff",
        "get_symbols", "go_to_definition", "find_references", "get_diagnostics", "get_hover",
        "search_workspace_symbols",
    }
    assert sum(kind is ExecutionKind.TRUSTED_FILE for kind in kinds.values()) == 11


@pytest.mark.parametrize("kind,handler", [
    (ExecutionKind.HOST_CONTROL, "_host"),
    (ExecutionKind.TRUSTED_FILE, "_file"),
    (ExecutionKind.TRUSTED_NETWORK, "_network"),
    (ExecutionKind.SANDBOXED_PROCESS, "_process"),
])
def test_runtime_routes_each_kind_through_dispatcher(kind, handler, monkeypatch):
    tool = type("DeclaredTool", (HostTool,), {"execution_kind": kind})()
    runtime = AgentRuntime(object(), [tool])
    calls = []

    def execute(selected, args):
        calls.append(selected)
        args.clear()
        return ToolResult(True, {"route": kind.value})

    monkeypatch.setattr(runtime.dispatcher, handler, execute)
    args = {"value": 42}
    response = runtime._execute(ToolCall("test", "example", args))
    assert json.loads(response.content)["data"]["route"] == kind.value
    assert calls == [tool] and args == {"value": 42}


def test_declared_process_without_adapter_never_runs_host_code():
    class ProcessTool(HostTool):
        execution_kind = ExecutionKind.SANDBOXED_PROCESS

        def execute(self, arguments):
            pytest.fail("Unisolated process tool called")

    runtime = AgentRuntime(object(), [ProcessTool()])
    response = runtime._execute(ToolCall("test", "example", {}))
    assert json.loads(response.content)["error"]["code"] == "SANDBOX_REQUIRED"


@pytest.mark.parametrize("tool_type", [RunCommandTool, RunPythonTool, GetExecutionEnvironmentTool,
                                       GitDiffTool, GitStatusTool])
def test_execution_allowed_flag_alone_cannot_bypass_isolation(tmp_path, monkeypatch, tool_type):
    tool = tool_type(tmp_path, execution_allowed=True)
    monkeypatch.setattr(tool, "execute", lambda *_: pytest.fail("Raw process tool executed"))
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    assert dispatcher.execute(tool.definition.name, {}).error_code == "SANDBOX_REQUIRED"


@pytest.mark.parametrize("tool_type", [GetExecutionEnvironmentTool, GitDiffTool, GitStatusTool])
def test_only_exact_restricted_local_implementations_have_compatibility_route(
    tmp_path, monkeypatch, tool_type,
):
    tool = tool_type(tmp_path, execution_allowed=False)
    monkeypatch.setattr(tool, "execute", lambda *_: ToolResult(True, {"local": True}))
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    assert dispatcher.execute(tool.definition.name, {}).data == {"local": True}
    # A subclass cannot inherit the special allowance by copying the declaration.
    child = type("PluginTool", (tool_type,), {
        "execution_kind": ExecutionKind.SANDBOXED_PROCESS,
    })(tmp_path, execution_allowed=False)
    dispatcher = ToolDispatcher()
    dispatcher.register(child)
    assert dispatcher.execute(child.definition.name, {}).error_code == "SANDBOX_REQUIRED"


@pytest.mark.parametrize("kind", [ExecutionKind.TRUSTED_FILE, ExecutionKind.SANDBOXED_PROCESS])
@pytest.mark.parametrize("adapter", ["native", "docker"])
def test_proxies_preserve_kind_and_execute_through_their_backend(tmp_path, kind, adapter):
    calls = []

    def execute(root, name, args):
        calls.append((root, name, args))
        return ToolResult(True, {"backend": adapter})

    backend = SimpleNamespace(workspace=tmp_path, execute=execute)
    if adapter == "native":
        tool = NativeTool(HostTool.definition, backend, kind)
    else:
        session = SimpleNamespace(workspace=tmp_path, backend=backend, guard=WritebackGuard())
        tool = SandboxedTool(HostTool.definition, session, execution_kind=kind)
    dispatcher = ToolDispatcher()
    dispatcher.register(tool)
    assert dispatcher.execute("example", {}).data == {"backend": adapter}
    assert calls == [(tmp_path, "example", {})]


@pytest.mark.parametrize("kind", [ExecutionKind.HOST_CONTROL, ExecutionKind.TRUSTED_NETWORK,
                                  "sandboxed_process", None])
def test_proxy_cannot_silently_reclassify_a_host_tool(kind):
    with pytest.raises(ValueError):
        NativeTool(HostTool.definition, None, kind)
    with pytest.raises(ValueError):
        SandboxedTool(HostTool.definition, None, execution_kind=kind)


def test_rule_changes_after_registration_fail_closed():
    tool = HostTool()
    runtime = AgentRuntime(object(), [tool])
    tool.execution_kind = ExecutionKind.TRUSTED_NETWORK
    response = runtime._execute(ToolCall("test", "example", {}))
    assert json.loads(response.content)["error"]["code"] == "TOOL_EXECUTION_ERROR"


@pytest.mark.parametrize("kind", [ExecutionKind.HOST_CONTROL, ExecutionKind.TRUSTED_NETWORK])
def test_worker_refuses_host_control_and_network_tools(kind):
    tool = type("HostOnlyTool", (HostTool,), {"execution_kind": kind})()
    with pytest.raises(ValueError, match="cannot be registered in a sandbox worker"):
        ToolDispatcher(inside_sandbox=True).register(tool)


def test_worker_runs_validated_process_and_rejects_model_route_override(
    tmp_path, monkeypatch, capsys,
):
    from sandbox import worker

    calls = []

    class ProcessTool(HostTool):
        execution_kind = ExecutionKind.SANDBOXED_PROCESS

        def execute(self, arguments):
            calls.append(arguments)
            return ToolResult(True, {"isolated": True})

    monkeypatch.setattr(worker, "create_default_tools", lambda *a, **kw: [ProcessTool()])
    worker.execute_request({"name": "example", "arguments": {},
                            "execution_kind": "host_control"}, tmp_path)
    assert json.loads(capsys.readouterr().out)["data"] == {"isolated": True}
    assert calls == [{}]  # Request metadata never changes the registered route.

    class MissingRule:
        definition = HostTool.definition

    monkeypatch.setattr(worker, "create_default_tools", lambda *a, **kw: [MissingRule()])
    with pytest.raises(ValueError, match="execution_kind"):
        worker.execute_request({"name": "example", "arguments": {}}, tmp_path)
