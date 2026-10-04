"""Read cancellation, aggregate work limits and casefold source coordinates."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from types import SimpleNamespace

import pytest

from host_support.cancellation import (
    RunCancelled,
    cancellation_scope,
    checkpoint,
    current_cancellation,
)
from host_support.read_budget import ReadLimits
from sandbox.session import SandboxedTool
from tools import ToolDispatcher
from tools import filesystem as fs
from tools._internal.file_access import FileAccess
from tools._internal.text_search import literal_span


@pytest.mark.parametrize(
    "text,query,expected",
    [
        ("aßb", "ss", (1, 2)),
        ("aßb", "sb", (1, 3)),
        ("İx", "\u0307x", (0, 2)),
        ("ﬃxyz", "fix", (0, 2)),
        ("ABC", "bc", (1, 3)),
        ("Straße", "STRASSE", (0, 6)),
        ("abc", "z", None),
    ],
)
def test_casefold_span(text, query, expected):
    assert literal_span(text, query, case_sensitive=False) == expected


def test_casefold_long_line_snippet_contains_actual_match(tmp_path):
    (tmp_path / "a").write_text("ß" * 1800 + "TARGET" + "x" * 3000)
    result = fs.SearchFilesTool(tmp_path, max_line_chars=80).execute(
        {
            "query": "target",
            "case_sensitive": False,
        }
    )
    assert result.success
    assert "TARGET" in result.data["matches"][0]["line"]


@pytest.mark.parametrize("native", [False, True])
def test_search_byte_budget_keeps_completed_matches(tmp_path, native):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("needle\n")
    tool = fs.SearchFilesTool(tmp_path, read_limits=ReadLimits(max_bytes=14))
    with FileAccess(tmp_path).activate() if native else nullcontext():
        result = tool.execute({"query": "needle"})
    assert result.success and result.data["truncated"]
    assert result.data["truncation_reason"] == "max_bytes"
    assert [m["path"] for m in result.data["matches"]] == ["a", "b"]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize(
    "tool_type,args",
    [
        (fs.SearchFilesTool, {"query": "absent", "glob": "*.py"}),
        (fs.FindFileTool, {"pattern": "**/*.py"}),
    ],
)
def test_nonmatching_directories_are_bounded(tmp_path, native, tool_type, args):
    for i in range(4):
        (tmp_path / str(i)).mkdir()
    tool = tool_type(tmp_path, read_limits=ReadLimits(max_directories=2))
    with FileAccess(tmp_path).activate() if native else nullcontext():
        result = tool.execute(args)
    assert result.data["truncation_reason"] == "max_directories"
    assert result.data["truncated"]


@pytest.mark.parametrize("native", [False, True])
def test_entry_budget_counts_filtered_names(tmp_path, native):
    for i in range(10):
        (tmp_path / f".hidden{i}").touch()
    tool = fs.SearchFilesTool(tmp_path, read_limits=ReadLimits(max_entries=3))
    with FileAccess(tmp_path).activate() if native else nullcontext():
        result = tool.execute({"query": "missing"})
    assert result.data["truncation_reason"] == "max_entries"


def test_batch_read_preserves_results_and_reports_unread_files(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("abc\n")
    result = fs.ReadFileTool(tmp_path, read_limits=ReadLimits(max_bytes=4)).execute(
        {
            "reads": [{"path": name} for name in ("a", "b", "c")],
        }
    )
    assert not result.success
    assert [r["success"] for r in result.data["results"]] == [True, False, False]
    assert result.data["results"][1]["data"]["next_start_line"] == 1
    assert result.data["results"][2]["error"]["code"] == "READ_BUDGET_EXCEEDED"


def test_search_deadline_preserves_partial_output(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("needle\n")
    (tmp_path / "b").write_text("needle\n")
    clock = [0.0]
    monkeypatch.setattr("host_support.read_budget.monotonic", lambda: clock[0])
    original = fs.literal_span

    def match(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 11
        return result

    monkeypatch.setattr(fs, "literal_span", match)
    result = fs.SearchFilesTool(tmp_path).execute({"query": "needle"})
    assert result.data["matches_returned"] == 1
    assert result.data["truncation_reason"] == "timeout"


@pytest.mark.parametrize("native", [False, True])
def test_readonly_dispatch_is_cancelled_before_next_file(tmp_path, monkeypatch, native):
    for name in ("a", "b"):
        (tmp_path / name).write_text("needle\n")
    reads = []
    original = fs.read_snapshot

    def read(*args, **kwargs):
        result = original(*args, **kwargs)
        reads.append(args[0])
        current_cancellation().cancel()
        return result

    monkeypatch.setattr(fs, "read_snapshot", read)
    dispatcher = ToolDispatcher()
    dispatcher.register(fs.SearchFilesTool(tmp_path))
    with cancellation_scope(), FileAccess(tmp_path).activate() if native else cancellation_scope():
        with pytest.raises(RunCancelled):
            dispatcher.execute("search_files", {"query": "needle"})
    assert len(reads) == 1


def test_docker_read_proxy_stays_serial_but_cancellable(tmp_path):
    tool = fs.ReadFileTool(tmp_path)

    def execute(*_):
        current_cancellation().cancel()
        checkpoint()
        pytest.fail("readonly proxy deferred cancellation")

    session = SimpleNamespace(
        workspace=tmp_path,
        backend=SimpleNamespace(execute=execute),
        guard=SimpleNamespace(needs_review=False),
    )
    proxy = SandboxedTool(
        tool.definition,
        session,
        execution_kind=tool.execution_kind,
        scheduling_policy=tool.scheduling_policy,
    )
    assert not proxy.scheduling_policy.parallel
    dispatcher = ToolDispatcher()
    dispatcher.register(proxy)
    with cancellation_scope(), pytest.raises(RunCancelled):
        dispatcher.execute("read_file", {"reads": [{"path": "a"}]})


def test_read_budgets_are_per_call_not_shared_between_threads(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("x" * 16)
    tool = fs.ReadFileTool(tmp_path, read_limits=ReadLimits(max_bytes=16))
    both = Barrier(2)
    original = tool._read_one

    def read(*args):
        both.wait(3)
        return original(*args)

    monkeypatch.setattr(tool, "_read_one", read)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(tool.execute, {"reads": [{"path": "a"}]}) for _ in range(2)]
        assert all(f.result(timeout=3).success for f in futures)
    assert tool.execute({"reads": []}).error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "options",
    [
        {"max_bytes": True},
        {"max_entries": 0},
        {"timeout_seconds": float("inf")},
        {"timeout_seconds": -1},
    ],
)
def test_invalid_limits(options):
    with pytest.raises(ValueError):
        ReadLimits(**options)


def test_factory_propagates_read_limits_without_enabling_processes(tmp_path):
    from tools import create_default_tools
    from tools.scheduling import WorkspaceAccess

    limits = ReadLimits(max_bytes=32)
    tools = create_default_tools(tmp_path, read_limits=limits)
    reads = [t for t in tools if t.scheduling_policy.workspace_access is WorkspaceAccess.READ]
    assert len(reads) == 5
    assert all(t.read_limits is limits for t in reads)
    assert "run_command" not in {t.definition.name for t in tools}
