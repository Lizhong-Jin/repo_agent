"""Tool visibility, request boundaries, authorization and extensible group catalogs."""

import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from session_helpers import Model
from test_runtime import RecordingTool, ScriptedLLM, reply

from agent import AgentRuntime
from agent.conversation import SavedConversation
from agent.session import SessionStore
from cli.session_status import SessionStatus
from llm import Message, ToolCall, ToolDefinition
from sandbox.native import NativeTool
from tools import DEFAULT_TOOL_GROUPS, ExecutionKind, ToolGroup, ToolResult, create_default_tools
from tools.tool_groups import LoadToolGroupTool, ToolGroupRegistry


def names(request):
    return {definition.name for definition in request.tools}


def result(message):
    return json.loads(message.content)


def test_default_catalog_partitions_current_tools(tmp_path):
    tools = create_default_tools(tmp_path, isolated_execution=True)
    runtime = AgentRuntime(ScriptedLLM([]), tools, tool_groups=DEFAULT_TOOL_GROUPS)
    initial = names(runtime._request([Message("user", "task")]))
    assert initial == {
        "get_execution_environment",
        "read_file",
        "list_files",
        "find_files",
        "search_files",
        "get_path_info",
        "load_tool_group",
    }
    all_names = {tool.definition.name for tool in tools}
    grouped = {name for group in DEFAULT_TOOL_GROUPS for name in group.tools}
    assert grouped <= all_names  # Catch typos/stale names in the central catalog.
    assert all_names - grouped == initial - {"load_tool_group"}


def test_load_enables_next_request_only_and_preserves_other_groups(tmp_path):
    model = ScriptedLLM(
        [
            reply(calls=[ToolCall("early", "write_file", {"path": "early", "content": "bad"})]),
            reply(
                calls=[
                    ToolCall("load", "load_tool_group", {"group": "file_editing"}),
                    ToolCall("same", "write_file", {"path": "same", "content": "bad"}),
                ]
            ),
            reply(calls=[ToolCall("write", "write_file", {"path": "ok", "content": "done"})]),
            reply("done"),
        ]
    )
    runtime = AgentRuntime(model, create_default_tools(tmp_path), tool_groups=DEFAULT_TOOL_GROUPS)
    baseline = runtime.estimate_context_tokens()
    answer = runtime.run("write a file")
    assert answer.status == "completed"
    assert "write_file" not in names(model.requests[0])
    assert "write_file" not in names(model.requests[1])
    assert "write_file" in names(model.requests[2])
    assert "git_diff" not in names(model.requests[2])
    assert result(model.requests[1].messages[-1])["error"]["code"] == "TOOL_NOT_LOADED"
    assert result(model.requests[2].messages[-1])["error"]["code"] == "TOOL_NOT_LOADED"
    assert not (tmp_path / "early").exists() and not (tmp_path / "same").exists()
    assert (tmp_path / "ok").read_text() == "done"
    assert runtime.estimate_context_tokens() > baseline
    # Requests already sent to the model are not mutated retroactively.
    assert "write_file" not in names(model.requests[0])


def test_local_loading_never_grants_execution(tmp_path):
    runtime = AgentRuntime(
        ScriptedLLM([]), create_default_tools(tmp_path), tool_groups=DEFAULT_TOOL_GROUPS
    )
    observation = runtime._execute(ToolCall("load", "load_tool_group", {"group": "coding"}))
    loaded = result(observation)
    assert loaded["success"]
    assert set(loaded["data"]["tools"]) == {"git_status", "git_diff"}
    unavailable = {
        "run_command",
        "run_shell",
        "run_python",
        "get_symbols",
        "go_to_definition",
        "find_references",
        "get_diagnostics",
        "get_hover",
        "search_workspace_symbols",
    }
    assert set(loaded["data"]["unavailable_tools"]) == unavailable
    assert unavailable.isdisjoint(names(runtime._request([Message("user", "task")])))
    for name in unavailable:
        assert name not in runtime._tools
        observation = runtime._execute(ToolCall(name, name, {}))
        assert result(observation)["error"]["code"] == "UNKNOWN_TOOL"
    assert runtime.loaded_tool_groups == ("coding",)


def test_group_without_any_registered_members_is_unavailable():
    registry = ToolGroupRegistry(DEFAULT_TOOL_GROUPS, ["read_file"])
    assert registry.load("coding").error_code == "TOOL_GROUP_UNAVAILABLE"
    assert registry.loaded == ()


def test_loading_appends_definitions_without_reordering_existing_prefix(tmp_path):
    runtime = AgentRuntime(
        ScriptedLLM([]), create_default_tools(tmp_path), tool_groups=DEFAULT_TOOL_GROUPS
    )
    initial = runtime._definitions
    assert runtime.tool_groups.load("coding").success
    coding_loaded = runtime._definitions
    assert coding_loaded[: len(initial)] == initial
    assert {tool.name for tool in coding_loaded[len(initial) :]} == {"git_status", "git_diff"}
    assert runtime.tool_groups.load("file_editing").success
    assert runtime._definitions[: len(coding_loaded)] == coding_loaded
    assert runtime.loaded_tool_groups == ("coding", "file_editing")
    assert runtime.restore_tool_groups(["coding", "file_editing"]) == ()
    assert runtime._definitions[: len(coding_loaded)] == coding_loaded


def test_custom_groups_movement_partial_availability_and_idempotence():
    record = RecordingTool()
    for group_name in ("coding", "literature"):
        runtime = AgentRuntime(
            ScriptedLLM([]),
            [record],
            tool_groups=[
                ToolGroup(group_name, "A custom capability", ("record", "not_installed")),
            ],
        )
        assert "record" not in names(runtime._request([Message("user", "task")]))
        first = runtime._execute(ToolCall("load", "load_tool_group", {"group": group_name}))
        second = runtime._execute(ToolCall("again", "load_tool_group", {"group": group_name}))
        assert result(first)["data"]["tools"] == ["record"]
        assert result(first)["data"]["unavailable_tools"] == ["not_installed"]
        assert not result(first)["data"]["already_loaded"]
        assert result(second)["data"]["already_loaded"]
        assert len(names(runtime._request([Message("user", "task")]))) == 2
        assert record.seen == []  # Loading never calls a grouped tool.


def test_proxy_loading_stays_in_host_and_execution_stays_in_backend(tmp_path):
    calls = []

    def execute(workspace, name, arguments):
        calls.append((workspace, name, arguments))
        return ToolResult(True, {"executed": name})

    backend = SimpleNamespace(workspace=tmp_path, execute=execute)
    proxy = NativeTool(
        ToolDefinition("run_command", "Test proxy"),
        backend,
        ExecutionKind.SANDBOXED_PROCESS,
    )
    runtime = AgentRuntime(ScriptedLLM([]), [proxy], tool_groups=DEFAULT_TOOL_GROUPS)
    loaded = runtime._execute(ToolCall("load", "load_tool_group", {"group": "coding"}))
    assert result(loaded)["success"]
    assert result(loaded)["data"]["tools"] == ["run_command"]
    assert not calls
    runtime._execute(ToolCall("run", "run_command", {"command": ["echo", "ok"]}))
    assert calls == [(tmp_path, "run_command", {"command": ["echo", "ok"]})]


@pytest.mark.parametrize(
    "arguments",
    [None, {}, {"group": []}, {"group": ""}, {"group": "coding", "execution_allowed": True}],
)
def test_loader_validates_arguments(arguments):
    registry = ToolGroupRegistry(DEFAULT_TOOL_GROUPS, ["git_diff"])
    assert LoadToolGroupTool(registry).execute(arguments).error_code == "INVALID_ARGUMENTS"
    assert registry.loaded == ()


def test_unknown_group_and_invalid_catalog_fail_clearly():
    registry = ToolGroupRegistry(DEFAULT_TOOL_GROUPS, [])
    assert registry.load("missing").error_code == "UNKNOWN_TOOL_GROUP"
    with pytest.raises(ValueError, match="multiple groups"):
        ToolGroupRegistry([ToolGroup("a", "A", ("x",)), ToolGroup("b", "B", ("x",))], [])
    with pytest.raises(ValueError, match="Duplicate tool group"):
        ToolGroupRegistry([ToolGroup("a", "A", ("x",)), ToolGroup("a", "B", ("y",))], [])
    with pytest.raises(ValueError, match="reserved"):
        ToolGroup("a", "A", ("load_tool_group",))


def test_legacy_runtime_and_unclassified_extensions_remain_available():
    tool = RecordingTool()
    legacy = AgentRuntime(ScriptedLLM([]), [tool])
    assert names(legacy._request([Message("user", "task")])) == {"record"}
    grouped = AgentRuntime(ScriptedLLM([]), [tool], tool_groups=DEFAULT_TOOL_GROUPS)
    assert names(grouped._request([Message("user", "task")])) == {"record", "load_tool_group"}


def test_sessions_restore_revalidate_and_reset_groups(tmp_path):
    root = tmp_path / "project"
    root.mkdir()

    def conversation(store, isolated, groups=DEFAULT_TOOL_GROUPS):
        model = Model()
        runtime = AgentRuntime(
            model, create_default_tools(root, isolated_execution=isolated), tool_groups=groups
        )
        return SavedConversation(
            store,
            runtime,
            model.config,
            SessionStatus(root),
            execution_mode="native" if isolated else "local",
        )

    with closing(SessionStore(root).open()) as store:
        first = conversation(
            store,
            True,
            (
                *DEFAULT_TOOL_GROUPS,
                ToolGroup("retired_group", "A group removed before resume", ("get_path_info",)),
            ),
        )
        assert first.runtime.restore_tool_groups(["file_editing", "coding", "retired_group"]) == ()
        first.checkpoint(strict=True)
    with closing(SessionStore(root).open()) as store:
        second = conversation(store, False)
        # coding survives through its local Git members, without restoring native capabilities.
        assert second.runtime.loaded_tool_groups == ("file_editing", "coding")
        assert "retired_group" in second.notice
        restored = names(second.runtime._request([Message("user", "task")]))
        assert {"git_status", "git_diff"} <= restored
        assert {"run_command", "run_python", "get_symbols"}.isdisjoint(restored)
        second.new_session()
        assert second.runtime.loaded_tool_groups == ()
        assert store.data["loaded_tool_groups"] == []
        assert second.runtime.restore_tool_groups(["coding"]) == ()
        assert second.runtime.loaded_tool_groups == ("coding",)
        second.clear()
        assert second.runtime.loaded_tool_groups == ()
        assert store.data["loaded_tool_groups"] == []


def test_restore_drops_removed_groups_and_reset_hides_tools():
    runtime = AgentRuntime(
        ScriptedLLM([]),
        [RecordingTool()],
        tool_groups=[
            ToolGroup("literature", "New group", ("record",)),
        ],
    )
    assert runtime.restore_tool_groups(["removed", "literature"]) == ("removed",)
    assert runtime.loaded_tool_groups == ("literature",)
    runtime.reset_tool_groups()
    assert "record" not in names(runtime._request([Message("user", "task")]))
