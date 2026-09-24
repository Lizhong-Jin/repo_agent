"""Patch correctness, concurrency, cleanup and bounded matching regressions."""

import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event, Thread

import pytest

from tools import ApplyPatchTool, WriteFileTool
from tools import filesystem as fs


def patch_text(*sections):
    return (
        "*** Begin Patch\n"
        + "".join(f"*** Update File: {path}\n{hunks}" for path, hunks in sections)
        + "*** End Patch\n"
    )


def run(root, *sections, **kwargs):
    return ApplyPatchTool(root).execute({"patch": patch_text(*sections), **kwargs})


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_multiple_files_and_hunks_preserve_encoding_permissions_and_hashes(tmp_path, bom, newline):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    original = bom + newline.join(["first", "middle", "last", ""]).encode()
    a.write_bytes(original)
    a.chmod(0o751)
    b.write_text("hello\n")
    result = run(
        tmp_path,
        ("a.txt", "@@\n-last\n+LAST\n@@\n-first\n+FIRST\n"),
        ("b.txt", "@@\n-hello\n+world\n"),
        expected_files=[{"path": "a.txt", "sha256": digest(original).upper()}],
    )
    assert result.success, result
    assert a.read_bytes() == bom + newline.join(["FIRST", "middle", "LAST", ""]).encode()
    assert b.read_bytes() == b"world\n"
    assert a.stat().st_mode & 0o777 == 0o751
    assert result.data["files_changed"] == 2 and result.data["hunks_applied"] == 3
    change = result.data["changes"][0]
    assert change["old_sha256"] == digest(original)
    assert change["new_sha256"] == digest(a.read_bytes())
    assert list(sorted(p.name for p in tmp_path.iterdir())) == ["a.txt", "b.txt"]


@pytest.mark.parametrize(
    ("before", "hunk", "after"),
    [
        ("a\nb", "@@\n-b\n", "a\n"),
        ("a\nb", "@@\n a\n-b\n", "a\n"),
        ("a\nb", "@@\n-b\n+B\n", "a\nB"),
        ("a\nb", "@@\n+B\n-a\n-b\n", "B"),
        ("a\nb\n", "@@\n-b\n", "a\n"),
        ("a", "@@\n a\n+b\n", "a\nb"),
        ("a\n", "@@\n a\n+b\n", "a\nb\n"),
        ("a\n\n", "@@\n \n+x\n", "a\n\nx\n"),
        ("a\n\n", "@@\n-\n", "a\n"),
        ("\n", "@@\n-\n+x\n", "x\n"),
        ("a\n", "@@\n-a\n", ""),
        ("a", "@@\n-a\n", ""),
        ("a\u2028b\n", "@@\n-a\u2028b\n+x\n", "x\n"),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_eof_and_real_line_boundaries(tmp_path, before, hunk, after, newline):
    p = tmp_path / "a.txt"
    p.write_bytes(before.replace("\n", newline).encode())
    result = run(tmp_path, ("a.txt", hunk))
    assert result.success, result
    # A file without any newline has no CR/CRLF convention to preserve.
    selected = newline if "\n" in before else "\n"
    assert p.read_bytes() == after.replace("\n", selected).encode()


def test_empty_file_has_no_phantom_anchor(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"")
    result = run(tmp_path, ("a.txt", "@@\n \n+x\n"))
    assert result.error_code == "CONTEXT_NOT_FOUND"
    assert p.read_bytes() == b""


@pytest.mark.parametrize("alias", ["./a.txt", "sub/../a.txt", "absolute", "directory_alias/a.txt"])
def test_duplicate_resolved_targets_are_rejected_before_staging(tmp_path, monkeypatch, alias):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x\ny\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "directory_alias").symlink_to(tmp_path, target_is_directory=True)
    alias = str(p) if alias == "absolute" else alias
    monkeypatch.setattr(fs, "NamedTemporaryFile", lambda **kw: pytest.fail("Staging started"))
    result = run(tmp_path, ("a.txt", "@@\n-x\n+X\n"), (alias, "@@\n-y\n+Y\n"))
    assert result.error_code == "DUPLICATE_FILE_SECTION"
    assert p.read_bytes() == b"x\ny\n"


def test_case_aliases_are_rejected_on_case_insensitive_filesystem(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes(b"x\ny\n")
    if not (tmp_path / "A.TXT").exists():
        pytest.skip("Case-sensitive filesystem")
    result = run(tmp_path, ("a.txt", "@@\n-x\n+X\n"), ("A.TXT", "@@\n-y\n+Y\n"))
    assert result.error_code == "DUPLICATE_FILE_SECTION"
    assert p.read_bytes() == b"x\ny\n"


@pytest.mark.parametrize("with_expected_hash", [False, True])
def test_concurrent_edit_of_later_file_stops_all_commits(tmp_path, monkeypatch, with_expected_hash):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    real_chmod = fs.os.chmod

    def edit_during_staging(path, mode):
        b.write_bytes(b"external edit\n")
        return real_chmod(path, mode)

    monkeypatch.setattr(fs.os, "chmod", edit_during_staging)
    kwargs = (
        {"expected_files": [{"path": "b.txt", "sha256": digest(b"b\n")}]}
        if with_expected_hash
        else {}
    )
    result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"), ("b.txt", "@@\n-b\n+B\n"), **kwargs)
    assert result.error_code == "FILE_CHANGED"
    assert result.data["committed"] == [] and result.data["not_committed"] == ["a.txt", "b.txt"]
    assert a.read_bytes() == b"a\n" and b.read_bytes() == b"external edit\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.txt"]


def test_same_content_new_inode_is_detected(tmp_path, monkeypatch):
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\n")
    real_chmod = fs.os.chmod

    def replace_during_staging(path, mode):
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"a\n")
        replacement.replace(p)
        return real_chmod(path, mode)

    monkeypatch.setattr(fs.os, "chmod", replace_during_staging)
    result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"))
    assert result.error_code == "FILE_CHANGED"
    assert p.read_bytes() == b"a\n"


@pytest.mark.parametrize("failure", [PermissionError, OSError, "concurrent_edit"])
def test_partial_commit_is_structured_and_remaining_files_preserved(tmp_path, monkeypatch, failure):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"a\n")
    b.write_bytes(b"b\n")
    replace = Path.replace

    def controlled_replace(source, target):
        if target == b and failure != "concurrent_edit":
            raise failure("simulated commit failure")
        result = replace(source, target)
        if target == a and failure == "concurrent_edit":
            b.write_bytes(b"external edit\n")
        return result

    monkeypatch.setattr(Path, "replace", controlled_replace)
    result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"), ("b.txt", "@@\n-b\n+B\n"))
    assert result.error_code == (
        "FILE_CHANGED" if failure == "concurrent_edit" else "MULTI_FILE_COMMIT_FAILED"
    )
    assert result.data["committed"] == ["a.txt"]
    assert result.data["not_committed"] == ["b.txt"]
    assert result.data["changes"][0]["new_sha256"] == digest(a.read_bytes())
    assert a.read_bytes() == b"A\n"
    assert b.read_bytes() == (b"external edit\n" if failure == "concurrent_edit" else b"b\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.txt"]


@pytest.mark.parametrize("stage", ["fsync", "chmod", "replace"])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit, PermissionError])
def test_failure_and_cancellation_clean_all_staged_files(tmp_path, monkeypatch, stage, exception):
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\n")

    def fail(*args):
        raise exception("simulated")

    monkeypatch.setattr(Path if stage == "replace" else fs.os, stage, fail)
    if exception is PermissionError:
        result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"))
        assert not result.success and result.data["committed"] == []
    else:
        with pytest.raises(exception):
            run(tmp_path, ("a.txt", "@@\n-a\n+A\n"))
    assert p.read_bytes() == b"a\n"
    assert list(tmp_path.iterdir()) == [p]


def test_matching_has_linear_comparison_bound_and_stops_after_two_matches():
    class Counted(str):
        comparisons = 0

        def __eq__(self, other):
            type(self).comparisons += 1
            return str.__eq__(self, other)

        def __ne__(self, other):
            return not self == other

    # A near-match at every position exercised the old quadratic slice loop.
    lines = [Counted("x")] * 20000
    pattern = tuple([Counted("x")] * 1000 + [Counted("missing")])
    assert ApplyPatchTool._find_subsequence(lines, pattern) == []
    assert Counted.comparisons < 6 * (len(lines) + len(pattern))
    assert ApplyPatchTool._find_subsequence(["x"] * 10000, ("x", "x")) == [0, 1]


def test_builtin_writers_are_serialized_with_patch_commit(tmp_path, monkeypatch):
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\n")
    staged, release, writer_started, writer_done = Event(), Event(), Event(), Event()
    chmod = fs.os.chmod
    results = []

    def pause(path, mode):
        staged.set()
        assert release.wait(3)
        return chmod(path, mode)

    def writer():
        writer_started.set()
        results.append(
            WriteFileTool(tmp_path).execute(
                {"path": "a.txt", "content": "writer\n", "overwrite": True}
            )
        )
        writer_done.set()

    monkeypatch.setattr(fs.os, "chmod", pause)
    patch_thread = Thread(target=lambda: results.append(run(tmp_path, ("a.txt", "@@\n-a\n+A\n"))))
    write_thread = Thread(target=writer)
    patch_thread.start()
    try:
        assert staged.wait(3)
        write_thread.start()
        assert writer_started.wait(3)
        assert not writer_done.wait(0.05)
    finally:
        release.set()
        patch_thread.join(3)
        if write_thread.ident:
            write_thread.join(3)
    assert len(results) == 2 and all(r.success for r in results)
    assert p.read_bytes() == b"writer\n"


@pytest.mark.parametrize(
    "kind,code",
    [
        ("outside", "PATH_OUTSIDE_WORKSPACE"),
        ("symlink", "PATH_IS_SYMLINK"),
        ("parent_escape", "PATH_OUTSIDE_WORKSPACE"),
        ("hardlink", "PROTECTED_FILE"),
        ("protected", "PROTECTED_FILE"),
    ],
)
def test_path_guards(tmp_path, kind, code):
    root = tmp_path / "workspace"
    root.mkdir()
    ordinary = root / "a.txt"
    ordinary.write_bytes(b"old\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"old\n")
    if kind == "outside":
        path = "../outside.txt"
    elif kind == "symlink":
        (root / "alias").symlink_to(ordinary)
        path = "alias"
    elif kind == "parent_escape":
        (root / "alias").symlink_to(tmp_path, target_is_directory=True)
        path = "alias/outside.txt"
    elif kind == "hardlink":
        os.link(outside, root / "alias")
        path = "alias"
    else:
        (root / ".env").write_bytes(b"old\n")
        path = ".env"
    result = run(root, (path, "@@\n-old\n+new\n"))
    assert result.error_code == code
    assert ordinary.read_bytes() == b"old\n" and outside.read_bytes() == b"old\n"


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"a\r\nb\n", "MIXED_LINE_ENDINGS"),
        (b"a\x00b", "BINARY_FILE"),
        (b"\xff", "UNSUPPORTED_ENCODING"),
    ],
)
def test_unsupported_content_is_unchanged(tmp_path, raw, code):
    p = tmp_path / "a.txt"
    p.write_bytes(raw)
    result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"))
    assert result.error_code == code and p.read_bytes() == raw


@pytest.mark.parametrize(
    "sections,code",
    [
        ([("a.txt", "@@\n-missing\n+A\n")], "CONTEXT_NOT_FOUND"),
        ([("a.txt", "@@\n-a\n+A\n@@\n a\n-b\n+B\n")], "OVERLAPPING_HUNKS"),
        ([("a.txt", "@@\n-a\n+a\n")], "NO_CHANGES"),
        ([("a.txt", "@@\n+new\n")], "INSERTION_WITHOUT_CONTEXT"),
        ([("a.txt", "@@\n-a\n+A\n"), ("absent.txt", "@@\n-x\n+y\n")], "FILE_NOT_FOUND"),
    ],
)
def test_preparation_errors_do_not_stage_or_modify_files(tmp_path, monkeypatch, sections, code):
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\nb\n")
    monkeypatch.setattr(fs, "NamedTemporaryFile", lambda **kw: pytest.fail("Staging started"))
    result = run(tmp_path, *sections)
    assert result.error_code == code and p.read_bytes() == b"a\nb\n"


def test_ambiguity_reports_bounded_candidate_lines(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x\n" * 100000)
    result = run(tmp_path, ("a.txt", "@@\n-x\n+y\n"))
    assert result.error_code == "AMBIGUOUS_CONTEXT"
    assert "[1, 2]" in result.error


@pytest.mark.parametrize(
    "option,limit,code",
    [
        ("max_patch_bytes", 10, "PATCH_TOO_LARGE"),
        ("max_content_bytes", 1, "FILE_TOO_LARGE"),
        ("max_content_bytes", 3, "PATCH_RESULT_TOO_LARGE"),
        ("max_files", 1, "TOO_MANY_PATCH_FILES"),
        ("max_hunks", 1, "TOO_MANY_HUNKS"),
        ("max_added_lines", 1, "TOO_MANY_ADDED_LINES"),
    ],
)
def test_limits_are_checked_before_writing(tmp_path, option, limit, code):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"a\n")
    tool = ApplyPatchTool(tmp_path, **{option: limit})
    result = tool.execute(
        {"patch": patch_text(("a.txt", "@@\n-a\n+long\n"), ("b.txt", "@@\n-a\n+long\n"))}
    )
    assert result.error_code == code
    assert all(p.read_bytes() == b"a\n" for p in tmp_path.iterdir())


@pytest.mark.parametrize(
    "expected,code",
    [
        ([{"path": "a.txt", "sha256": "0" * 64}], "FILE_CHANGED"),
        ([{"path": "unused", "sha256": "0" * 64}], "INVALID_ARGUMENTS"),
        ([{"path": "a.txt", "sha256": "bad"}], "INVALID_ARGUMENTS"),
        ([{"path": "a.txt", "sha256": "0" * 64}] * 2, "INVALID_ARGUMENTS"),
    ],
)
def test_expected_files_checks(tmp_path, expected, code):
    p = tmp_path / "a.txt"
    p.write_bytes(b"a\n")
    result = run(tmp_path, ("a.txt", "@@\n-a\n+A\n"), expected_files=expected)
    assert result.error_code == code and p.read_bytes() == b"a\n"


def test_cancel_after_first_commit_cleans_remaining_temps(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"a\n")
    replace = Path.replace

    def cancel_second(source, target):
        if target.name == "b.txt":
            raise KeyboardInterrupt
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", cancel_second)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, ("a.txt", "@@\n-a\n+A\n"), ("b.txt", "@@\n-a\n+A\n"))
    assert (tmp_path / "a.txt").read_bytes() == b"A\n"
    assert (tmp_path / "b.txt").read_bytes() == b"a\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.txt"]


def test_worker_preserves_structured_partial_commit(tmp_path, monkeypatch, capsys):
    from sandbox.worker import execute_request

    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"a\n")
    replace = Path.replace

    def fail_second(source, target):
        if target.name == "b.txt":
            raise PermissionError("simulated")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    execute_request(
        {
            "name": "apply_patch",
            "arguments": {
                "patch": patch_text(("a.txt", "@@\n-a\n+A\n"), ("b.txt", "@@\n-a\n+A\n"))
            },
        },
        str(tmp_path),
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "MULTI_FILE_COMMIT_FAILED"
    assert payload["data"]["committed"] == ["a.txt"]
    assert payload["data"]["not_committed"] == ["b.txt"]
    assert payload["data"]["changes"][0]["new_sha256"] == digest(b"A\n")


@pytest.mark.skipif(
    not (
        (sys.platform == "darwin" and os.getenv("RUN_SANDBOX_NATIVE_TESTS") == "1")
        or (sys.platform == "linux" and os.getenv("RUN_SANDBOX_LINUX_TESTS") == "1")
    ),
    reason="Opt-in real native sandbox test",
)
def test_native_patch_commit_and_validation_failure_preserve_health(tmp_path):
    from sandbox.native import NativeBackend

    (tmp_path / "a.txt").write_bytes(b"a\r\nb")
    (tmp_path / "b.txt").write_bytes(b"x\n")
    backend = NativeBackend(tmp_path)
    try:
        result = backend.execute(
            tmp_path,
            "apply_patch",
            {"patch": patch_text(("a.txt", "@@\n-b\n"), ("b.txt", "@@\n-x\n+X\n"))},
        )
        assert result.success and result.data["files_changed"] == 2, result
        assert (tmp_path / "a.txt").read_bytes() == b"a\r\n"
        assert (tmp_path / "b.txt").read_bytes() == b"X\n"
        result = backend.execute(
            tmp_path,
            "apply_patch",
            {"patch": patch_text(("a.txt", "@@\n-a\n+A\n"), ("./a.txt", "@@\n-a\n+B\n"))},
        )
        assert result.error_code == "DUPLICATE_FILE_SECTION" and backend.healthy
        assert (tmp_path / "a.txt").read_bytes() == b"a\r\n"
    finally:
        backend.close()
