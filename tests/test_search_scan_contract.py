"""Search behavior, bounded reader lifetimes, work avoided and race protection."""

import os
from contextlib import contextmanager

import pytest

from host_support import filesystem as host_fs
from tools._internal.file_access import FileAccess
from tools._internal.text_search import text_lines
from tools.filesystem import SearchFilesTool


@pytest.mark.parametrize(
    "text", ["", "a", "\r\n", "a\r\nb\rc\n", "a\v\fb\x85c\u2028d\u2029", "\n\r\r\nlast"]
)
def test_lazy_lines_preserve_exact_search_line_semantics(text):
    assert list(text_lines(text)) == text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


@pytest.mark.parametrize(
    "options", [{}, {"max_results": 1}, {"max_files_scanned": 1}, {"max_output_chars": 4}]
)
@pytest.mark.parametrize("case_sensitive", [False, True])
def test_native_search_matches_local_order_encoding_lines_and_limits(
    tmp_path, options, case_sensitive
):
    root = tmp_path.resolve()
    (root / "sub").mkdir()
    (root / "A.txt").write_bytes(b"\xef\xbb\xbfneedle\r\nNEEDLE\rneedle\n")
    (root / "b.txt").write_text("no match\n")
    (root / "sub/c.txt").write_text("needle\vneedle\u2028needle\n")
    (root / "sub/binary").write_bytes(b"needle\x00")
    (root / "sub/invalid").write_bytes(b"needle\xff")
    tool = SearchFilesTool(root, **options)
    args = {"query": "needle", "case_sensitive": case_sensitive}
    expected = tool.execute(args)
    with FileAccess(root).activate():
        actual = tool.execute(args)
    assert actual == expected


def test_casefold_and_nonstandard_unicode_line_separators(tmp_path):
    root = tmp_path.resolve()
    (root / "text").write_text("first\r\nStraße\vStraße\u2028Straße\rlast")
    with FileAccess(root).activate():
        result = SearchFilesTool(root).execute({"query": "STRASSE", "case_sensitive": False})
    assert result.data["matches"] == [
        {"path": "text", "line_number": 2, "line": "Straße\vStraße\u2028Straße"}
    ]


@pytest.mark.parametrize("files", [5, 100])
def test_directory_opens_do_not_grow_with_files_in_same_directory(tmp_path, monkeypatch, files):
    root = tmp_path.resolve()
    parent = root / "src/package"
    parent.mkdir(parents=True)
    for index in range(files):
        (parent / f"{index}.txt").write_text("ordinary\n")
    calls = []
    original = host_fs.open_directory

    def opened(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(host_fs, "open_directory", opened)
    with FileAccess(root).activate():
        result = SearchFilesTool(root).execute({"query": "absent"})
    assert result.success and result.data["files_scanned"] == files
    assert len(calls) <= 5  # root + its two nested directory paths, not per-file opens.


@pytest.mark.parametrize("stop", ["results", "files", "output", "exception"])
def test_search_closes_directory_readers_when_stopping_early(tmp_path, monkeypatch, stop):
    root = tmp_path.resolve()
    for index in range(3):
        (root / f"{index}.txt").write_text("needle\nneedle\n")
    options = {
        "results": {"max_results": 1},
        "files": {"max_files_scanned": 1},
        "output": {"max_output_chars": 1},
        "exception": {},
    }[stop]
    readers = []
    original = FileAccess.read_directory

    @contextmanager
    def read(self, path):
        with original(self, path) as reader:
            readers.append(reader)
            yield reader

    monkeypatch.setattr(FileAccess, "read_directory", read)
    if stop == "exception":

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(SearchFilesTool, "_truncate_matching_line", interrupted)
    with FileAccess(root).activate():
        if stop == "exception":
            with pytest.raises(KeyboardInterrupt):
                SearchFilesTool(root, **options).execute({"query": "needle"})
        else:
            assert SearchFilesTool(root, **options).execute({"query": "needle"}).data["truncated"]
    assert readers
    for reader in readers:
        with pytest.raises(ValueError, match="closed"):
            reader.names()
        with pytest.raises(OSError):
            os.fstat(reader.fd)


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "file", "parent"])
def test_native_search_rechecks_opened_files_after_enumeration(tmp_path, monkeypatch, replacement):
    root = (tmp_path / "workspace").resolve()
    parent = root / "src"
    parent.mkdir(parents=True)
    target = parent / "file.txt"
    target.write_text("ordinary")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "file.txt"
    secret.write_text("SYNTHETIC_SECRET")
    original = host_fs.open_file
    changed = False

    def open_file(name, *args, **kwargs):
        nonlocal changed
        if name == target.name and not changed:
            changed = True
            if replacement == "parent":
                parent.rename(root / "old-src")
                parent.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                if replacement == "symlink":
                    target.symlink_to(secret)
                elif replacement == "hardlink":
                    target.hardlink_to(secret)
                else:
                    target.write_text("SYNTHETIC_SECRET")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(host_fs, "open_file", open_file)
    with FileAccess(root).activate():
        result = SearchFilesTool(root).execute({"query": "SYNTHETIC_SECRET"})
    assert changed and result.success
    assert result.data["matches"] == []


def test_no_match_avoids_line_iteration_but_still_validates_encoding(tmp_path, monkeypatch):
    from tools import filesystem

    root = tmp_path.resolve()
    (root / "normal").write_text("ordinary\n" * 1000)
    (root / "invalid").write_bytes(b"ordinary\xff")
    monkeypatch.setattr(
        filesystem, "text_lines", lambda text: pytest.fail("unneeded line iteration")
    )
    result = SearchFilesTool(root).execute({"query": "absent"})
    assert result.success and result.data["skipped_files"] == 1
