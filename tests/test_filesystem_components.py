"""Regression coverage for shared I/O and work avoided during filesystem operations."""

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from tools import filesystem as fs
from tools._internal import file_policy

DIRECTORY_TOOLS = [
    (fs.ListFileTool, {"path": "."}, "entries"),
    (fs.FindFileTool, {"pattern": "*.txt"}, "matches"),
    (fs.SearchFilesTool, {"query": "needle"}, "matches"),
]


@pytest.mark.parametrize("tool_type,arguments,result_key", DIRECTORY_TOOLS)
def test_directory_queries_reuse_metadata_and_refresh_policy(
    tmp_path,
    monkeypatch,
    tool_type,
    arguments,
    result_key,
):
    paths = {tmp_path / f"file_{i}.txt" for i in range(12)}
    for path in paths:
        path.write_text("needle\n")
    tool = tool_type(tmp_path)
    stat_calls = Counter()
    policy_calls = 0
    resolving = 0
    original_stat = Path.stat
    original_resolve = Path.resolve
    original_policy = file_policy.runtime_protected_paths

    def stat(path, *args, **kwargs):
        # Some Python versions stat inside resolve; count explicit metadata queries.
        if not resolving and path in paths:
            stat_calls[path] += 1
        return original_stat(path, *args, **kwargs)

    def resolve(path, *args, **kwargs):
        nonlocal resolving
        resolving += 1
        try:
            return original_resolve(path, *args, **kwargs)
        finally:
            resolving -= 1

    def protected_paths(*args, **kwargs):
        nonlocal policy_calls
        policy_calls += 1
        return original_policy(*args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(file_policy, "runtime_protected_paths", protected_paths)
    result = tool.execute(arguments)
    assert result.success, result
    assert {item["path"] for item in result.data[result_key]} == {p.name for p in paths}
    assert stat_calls == Counter({path: 1 for path in paths})
    assert policy_calls == 1

    # Reusing the tool must observe both environment changes and new hard links.
    protected = tmp_path / "file_0.txt"
    monkeypatch.setenv("AGENT_ENV_FILE", str(protected))
    (tmp_path / "alias.txt").symlink_to(protected)
    (tmp_path / "hardlink.txt").hardlink_to(tmp_path / "file_1.txt")
    result = tool.execute(arguments)
    assert result.success, result
    assert policy_calls == 2
    names = {item["path"] for item in result.data[result_key]}
    assert names == {p.name for p in paths} - {"file_0.txt", "file_1.txt"}


def test_patch_parses_each_file_once_but_verifies_it_twice(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"\xef\xbb\xbfhello\r\n")
    tool = fs.ApplyPatchTool(tmp_path)
    reads, parses = Counter(), Counter()
    original_read = tool._read_snapshot
    original_detect = tool._detect_newline

    def read(path):
        reads[path] += 1
        return original_read(path)

    def detect(text, path):
        parses[path] += 1
        return original_detect(text, path)

    monkeypatch.setattr(tool, "_read_snapshot", read)
    monkeypatch.setattr(tool, "_detect_newline", detect)
    result = tool.execute(
        {
            "patch": "*** Begin Patch\n"
            "*** Update File: a.txt\n@@\n-hello\n+world\n"
            "*** Update File: b.txt\n@@\n-hello\n+world\n"
            "*** End Patch\n",
        }
    )
    assert result.success, result
    assert reads == {"a.txt": 3, "b.txt": 3}
    assert parses == {"a.txt": 1, "b.txt": 1}
    for name in ("a.txt", "b.txt"):
        assert (tmp_path / name).read_bytes() == b"\xef\xbb\xbfworld\r\n"


def test_patch_revalidation_checks_bytes_even_with_identical_metadata(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_bytes(b"hello\n")
    tool = fs.ApplyPatchTool(tmp_path)
    loaded = tool._load_file("a.txt")
    snapshot = tool._read_snapshot("a.txt")
    # Model a filesystem returning unchanged metadata for different content.
    changed = replace(snapshot, raw=b"HELLO\n")
    monkeypatch.setattr(tool, "_read_snapshot", lambda path: changed)
    assert tool._check_unchanged(loaded).error_code == "FILE_CHANGED"


@pytest.mark.parametrize(
    "tool_type,arguments",
    [
        (fs.WriteFileTool, {"path": "a.txt", "content": "world", "overwrite": True}),
        (
            fs.EditFileTool,
            {
                "path": "a.txt",
                "edits": [{"old_text": "hello", "new_text": "world"}],
            },
        ),
    ],
)
def test_shared_writer_cleans_up_on_cancellation(tmp_path, monkeypatch, tool_type, arguments):
    target = tmp_path / "a.txt"
    target.write_text("hello")

    def cancel(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(fs.os, "fsync", cancel)
    with pytest.raises(KeyboardInterrupt):
        tool_type(tmp_path).execute(arguments)
    assert target.read_text() == "hello"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("tool_type", [fs.ListFileTool, fs.FindFileTool])
def test_directory_metadata_preserves_symlink_types_and_target_protection(
    tmp_path,
    monkeypatch,
    tool_type,
):
    (tmp_path / "ordinary.txt").write_text("hello")
    (tmp_path / "link.txt").symlink_to(tmp_path / "ordinary.txt")
    (tmp_path / "dangling.txt").symlink_to(tmp_path / "missing.txt")
    protected = tmp_path / "custom.txt"
    protected.write_text("private")
    (tmp_path / "protected_alias.txt").symlink_to(protected)
    monkeypatch.setenv("AGENT_ENV_FILE", str(protected))
    arguments = {"path": "."} if tool_type is fs.ListFileTool else {"pattern": "*.txt"}
    result = tool_type(tmp_path).execute(arguments)
    assert result.success, result
    entries = result.data["entries" if tool_type is fs.ListFileTool else "matches"]
    assert {item["path"]: item["type"] for item in entries} == {
        "ordinary.txt": "file",
        "link.txt": "symlink",
        "dangling.txt": "symlink",
    }
    assert next(item for item in entries if item["type"] == "file")["size"] == 5
