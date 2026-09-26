"""Both native platforms share a trusted file layer; process tools stay isolated."""

import json
import os
import sys
from pathlib import Path

import pytest

from sandbox.linux_native import LinuxNativeBackend
from sandbox.native import NativeBackend
from tools._internal.base import ExecutionKind
from tools._internal.file_access import FileAccess, current_file_access
from tools._internal.process_runner import ProcessResult
from tools.factory import create_file_tools


@pytest.fixture(params=[NativeBackend, LinuxNativeBackend], ids=["macos", "linux"])
def backend(tmp_path, request, monkeypatch):
    backend = object.__new__(request.param)
    backend.workspace = tmp_path / "project"
    backend.workspace.mkdir()
    backend.python = Path(sys.executable)
    backend.read_paths = ()
    backend.protected_paths = ()
    backend.healthy = True
    backend.last_cleanup = {}
    monkeypatch.setattr(backend, "_run", lambda *a, **kw: pytest.fail("OS sandbox launched"))
    monkeypatch.setattr(backend, "_check_workspace", lambda: pytest.fail("Full workspace scanned"))
    return backend


def call(backend, name, **arguments):
    return backend.execute(backend.workspace, name, arguments)


def test_all_file_tools_work_without_processes_or_global_scans(backend, monkeypatch):
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("Host process launched"))
    monkeypatch.setattr(os, "walk", lambda *a, **kw: pytest.fail("Unconfined traversal"))
    tools = {tool.definition.name: tool for tool in backend.tools()}
    files = {tool.definition.name for tool in create_file_tools(backend.workspace)}
    assert len(files) == 11
    assert {name for name, tool in tools.items()
            if tool.execution_kind is ExecutionKind.TRUSTED_FILE} == files
    for name in files:
        assert "execution_kind" not in tools[name].definition.parameters["properties"]
    assert tools["run_python"].execution_kind is ExecutionKind.SANDBOXED_PROCESS
    assert call(backend, "make_directory", path="src/nested", parents=True).success
    assert call(backend, "write_file", path="src/a.txt", content="hello\n").success
    assert call(backend, "read_file", reads=[{"path": "src/a.txt"}]).success
    assert call(backend, "edit_file", path="src/a.txt",
                edits=[{"old_text": "hello", "new_text": "world"}]).success
    result = call(backend, "apply_patch", patch=(
        "*** Begin Patch\n*** Update File: src/a.txt\n@@\n-world\n+changed\n*** End Patch\n"))
    assert result.success, result
    assert call(backend, "list_files", path="src").success
    found = call(backend, "find_files", pattern="**/*.txt")
    assert found.success and [m["path"] for m in found.data["matches"]] == ["src/a.txt"]
    searched = call(backend, "search_files", query="changed")
    assert searched.success and len(searched.data["matches"]) == 1
    assert call(backend, "get_path_info", path="src/a.txt").data["type"] == "file"
    assert call(backend, "move_file", source="src/a.txt", destination="dst/a.txt",
                create_parents=True).success
    assert (backend.workspace / "dst/a.txt").read_text() == "changed\n"
    assert call(backend, "delete_file", path="dst/a.txt").success
    assert not (backend.workspace / "dst/a.txt").exists()
    assert current_file_access() is None


@pytest.mark.parametrize("name", ["git_status", "git_diff", "get_symbols", "get_diagnostics",
                                  "get_execution_environment"])
def test_non_file_tools_keep_isolated_worker(backend, monkeypatch, name):
    calls = []
    monkeypatch.setattr(backend, "_check_workspace", lambda: calls.append("scan"))

    def run(**kwargs):
        calls.append(kwargs)
        return ProcessResult(0, json.dumps({"success": True, "data": {}}), "", False,
                             None, 1, False, False, cleanup_status="confirmed")

    monkeypatch.setattr(backend, "_run", run)
    assert call(backend, name).success
    assert calls[-1]["request"]["name"] == name
    # Linux validates inside the real _run policy scan (mocked in this test).
    assert calls[:-1] == ([] if isinstance(backend, LinuxNativeBackend) else ["scan"])
    assert current_file_access() is None


def test_unregistered_native_tool_is_rejected_without_scanning_or_spawning(backend):
    assert call(backend, "unknown_plugin_tool").error_code == "UNKNOWN_TOOL"


@pytest.mark.parametrize("name,args", [
    ("run_command", {"command": ["/bin/echo", "hello"]}),
    ("run_python", {"code": "print('hello')"}),
])
def test_execution_tools_keep_isolated_runner(backend, monkeypatch, name, args):
    calls = []
    monkeypatch.setattr(backend, "_check_workspace", lambda: calls.append("scan"))

    def run(command, **kwargs):
        calls.append(command)
        return ProcessResult(0, "hello\n", "", False, None, 1, False, False,
                             cleanup_status="confirmed")

    monkeypatch.setattr(backend, "_run", run)
    result = backend.execute(backend.workspace, name, args)
    assert result.success and result.data["stdout"] == "hello\n"
    assert calls[:-1] == ([] if isinstance(backend, LinuxNativeBackend) else ["scan"])


@pytest.mark.parametrize("path", [".env", ".git/config", "nested/key.pem", "logs/file.txt",
                                  ".agents/instructions", "../outside.txt"])
def test_protected_and_outside_paths_fail_in_lightweight_layer(backend, path):
    root = backend.workspace
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SYNTHETIC_SECRET")
    assert not call(backend, "read_file", reads=[{"path": path}]).success
    assert not call(backend, "write_file", path=path, content="bad", overwrite=True).success
    assert target.read_text() == "SYNTHETIC_SECRET"
    result = call(backend, "search_files", query="SYNTHETIC_SECRET", include_hidden=True)
    assert result.success and not result.data["matches"]


def test_backend_configured_paths_and_embedded_runtime_are_preserved(backend, monkeypatch):
    root = backend.workspace
    private = root / "private-config"
    private.write_text("SYNTHETIC_SECRET")
    backend.protected_paths = (private,)
    runtime = root / "runtime"
    runtime.mkdir()
    module = runtime / "module.py"
    module.write_text("trusted")
    backend.read_paths = (runtime,)
    (root / "alias").symlink_to(module)
    assert not call(backend, "read_file", reads=[{"path": "private-config"}]).success
    assert call(backend, "read_file", reads=[{"path": "runtime/module.py"}]).success
    for path in ("runtime/module.py", "alias"):
        assert not call(backend, "write_file", path=path, content="bad", overwrite=True).success
    assert not call(backend, "delete_file", path="runtime/module.py").success
    assert not call(backend, "move_file", source="runtime/module.py", destination="stolen").success
    assert not call(backend, "make_directory", path="runtime/new").success
    assert not call(backend, "write_file", path="runtime/new/deep/file", content="bad",
                    create_parents=True).success
    assert not (runtime / "new").exists()
    monkeypatch.setenv("AGENT_ENV_FILE", "future/config")
    assert not call(backend, "write_file", path="future/config", content="bad",
                    create_parents=True).success
    assert module.read_text() == "trusted"


def test_links_special_files_and_model_override_are_rejected(backend):
    root = backend.workspace
    secret = root.parent / "outside"
    secret.write_text("SYNTHETIC_SECRET")
    (root / "symlink").symlink_to(secret)
    (root / "hardlink").hardlink_to(secret)
    os.mkfifo(root / "fifo")
    for path in ("symlink", "hardlink", "fifo"):
        assert not call(backend, "read_file", reads=[{"path": path}]).success
        assert not call(backend, "write_file", path=path, content="bad", overwrite=True).success
    # An unrelated hard link/FIFO no longer disables all ordinary file operations.
    assert call(backend, "write_file", path="ok", content="allowed").success
    assert not call(backend, "write_file", path=".env", content="bad",
                    requires_os_sandbox=False).success
    assert not call(backend, "read_file", reads=[{"path": "outside"}],
                    execution_kind="trusted_file").success
    assert secret.read_text() == "SYNTHETIC_SECRET"


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "fifo", "parent_symlink"])
def test_read_rechecks_actual_opened_object(backend, monkeypatch, replacement):
    root = backend.workspace
    (root / "src").mkdir()
    target = root / "src/file"
    target.write_text("safe")
    outside = root.parent / "outside-dir"
    outside.mkdir()
    (outside / "file").write_text("SYNTHETIC_SECRET")
    original = os.open
    changed = False

    def race(path, flags, *args, **kwargs):
        nonlocal changed
        trigger = "src" if replacement == "parent_symlink" else "file"
        if path == trigger and "dir_fd" in kwargs and not changed:
            changed = True
            if replacement == "parent_symlink":
                target.parent.rename(root / "old-src")
                (root / "src").symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                if replacement == "symlink":
                    target.symlink_to(outside / "file")
                elif replacement == "hardlink":
                    target.hardlink_to(outside / "file")
                else:
                    os.mkfifo(target)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    result = call(backend, "read_file", reads=[{"path": "src/file"}])
    assert changed and not result.success
    assert "SYNTHETIC_SECRET" not in str(result)


def test_write_rejects_parent_replacement_and_cleans_pinned_staging(backend, monkeypatch):
    root = backend.workspace
    parent = root / "src"
    parent.mkdir()
    (parent / "file").write_text("safe")
    outside = root.parent / "outside-dir"
    outside.mkdir()
    (outside / "file").write_text("SYNTHETIC_SECRET")
    original = FileAccess.stage

    def race(self, target, content, mode):
        staged = original(self, target, content, mode)
        parent.rename(root / "old-src")
        parent.symlink_to(outside, target_is_directory=True)
        return staged

    monkeypatch.setattr(FileAccess, "stage", race)
    result = call(backend, "write_file", path="src/file", content="bad", overwrite=True)
    assert not result.success
    assert (outside / "file").read_text() == "SYNTHETIC_SECRET"
    assert list((root / "old-src").iterdir()) == [root / "old-src/file"]
    assert current_file_access() is None


def test_cancelled_write_cleans_staging_and_policy_context(backend, monkeypatch):
    (backend.workspace / "file").write_text("safe")

    def cancel(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", cancel)
    with pytest.raises(KeyboardInterrupt):
        call(backend, "write_file", path="file", content="bad", overwrite=True)
    assert (backend.workspace / "file").read_text() == "safe"
    assert [p.name for p in backend.workspace.iterdir()] == ["file"]
    assert current_file_access() is None


def test_unhealthy_backend_keeps_reads_but_blocks_all_writes(backend):
    (backend.workspace / "file").write_text("safe")
    backend.healthy = False
    result = call(backend, "read_file", reads=[{"path": "file"}])
    assert result.success and result.data["execution_allowed"] is False
    for name in (
        "write_file", "edit_file", "apply_patch", "make_directory", "delete_file", "move_file",
    ):
        assert call(backend, name).error_code == "NATIVE_UNHEALTHY"
    assert not backend.healthy


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "src/**", "src/**/", "**/**",
                                   "src/**/*.txt", "src/[ab].txt", "./src/*"])
def test_lightweight_glob_preserves_pathlib_matching(backend, pattern):
    root = backend.workspace
    (root / "src/nested").mkdir(parents=True)
    for name in ("a.txt", "src/a.txt", "src/nested/b.py"):
        (root / name).write_text("ordinary")
    expected = {p.relative_to(root).as_posix() for p in root.glob(pattern)}
    result = call(backend, "find_files", pattern=pattern)
    assert result.success
    assert {p["path"] for p in result.data["matches"]} == expected


@pytest.mark.parametrize("name,args", [
    ("list_files", {"path": "src"}),
    ("find_files", {"path": "src", "pattern": "**/*"}),
    ("search_files", {"path": "src", "query": "SYNTHETIC_SECRET"}),
])
def test_directory_swap_never_enumerates_external_tree(backend, monkeypatch, name, args):
    root = backend.workspace
    (root / "src").mkdir()
    (root / "src/safe").write_text("ordinary")
    outside = root.parent / "outside-dir"
    outside.mkdir()
    (outside / "SYNTHETIC_SECRET_NAME").write_text("SYNTHETIC_SECRET")
    original = FileAccess.iterdir
    changed = False

    def race(self, path):
        nonlocal changed
        if not changed:
            changed = True
            (root / "src").rename(root / "old-src")
            (root / "src").symlink_to(outside, target_is_directory=True)
        yield from original(self, path)

    monkeypatch.setattr(FileAccess, "iterdir", race)
    result = backend.execute(root, name, args)
    assert changed
    assert "SYNTHETIC_SECRET_NAME" not in str(result)
    if name == "search_files":
        assert result.data["matches"] == []


def test_native_file_layer_respects_tool_group_loading(backend):
    from test_runtime import ScriptedLLM

    from agent.runtime import AgentRuntime
    from llm import ToolCall
    from tools.tool_groups import DEFAULT_TOOL_GROUPS

    runtime = AgentRuntime(ScriptedLLM([]), backend.tools(), tool_groups=DEFAULT_TOOL_GROUPS)
    args = {"path": "new.txt", "content": "ok"}
    blocked = runtime._execute(ToolCall("before", "write_file", args))
    assert json.loads(blocked.content)["error"]["code"] == "TOOL_NOT_LOADED"
    assert not (backend.workspace / "new.txt").exists()
    runtime._execute(ToolCall("load", "load_tool_group", {"group": "file_editing"}))
    result = runtime._execute(ToolCall("after", "write_file", args))
    assert json.loads(result.content)["success"]
    assert (backend.workspace / "new.txt").read_text() == "ok"
