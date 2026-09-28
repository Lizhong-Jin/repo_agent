from pathlib import Path

import pytest

from tools import filesystem
from tools.filesystem import SearchFilesTool


def write_file(root, name, content="needle\n"):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


@pytest.mark.parametrize("include_hidden", [False, True])
def test_recursive_search_never_opens_credentials(tmp_path, monkeypatch, include_hidden):
    write_file(tmp_path, "visible.txt")
    protected = [
        write_file(tmp_path, name, "needle=PRIVATE_VALUE\n")
        for name in [".env", ".env.local", ".env.example", "sub/.env", "config.txt"]
    ]
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "config.txt"))
    (tmp_path / "alias.txt").symlink_to(tmp_path / "config.txt")
    opened = []
    original_open = Path.open

    def tracked_open(path, *args, **kwargs):
        opened.append(path.resolve())
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_open)
    result = SearchFilesTool(tmp_path).execute(
        {"query": "needle", "include_hidden": include_hidden}
    )

    assert result.success
    assert [match["path"] for match in result.data["matches"]] == ["visible.txt"]
    assert not set(protected).intersection(opened)
    assert "PRIVATE_VALUE" not in str(result)


def test_protected_directory_is_pruned(tmp_path, monkeypatch):
    write_file(tmp_path, ".env.local/nested/private.txt", "needle=PRIVATE_VALUE\n")
    write_file(tmp_path, "visible.txt")
    visited = []
    original_walk = filesystem.os.walk

    def tracked_walk(*args, **kwargs):
        for root, directories, files in original_walk(*args, **kwargs):
            visited.append(Path(root))
            yield root, directories, files

    monkeypatch.setattr(filesystem.os, "walk", tracked_walk)
    result = SearchFilesTool(tmp_path).execute({"query": "needle", "include_hidden": True})

    assert result.success
    assert visited == [tmp_path]
    assert [match["path"] for match in result.data["matches"]] == ["visible.txt"]
    assert "PRIVATE_VALUE" not in str(result)


@pytest.mark.parametrize("path", [".env.local", ".env.local/nested/private.txt"])
@pytest.mark.parametrize("include_hidden", [False, True])
def test_explicit_protected_directory_or_descendant_is_rejected(tmp_path, path, include_hidden):
    write_file(tmp_path, ".env.local/nested/private.txt", "needle=PRIVATE_VALUE\n")

    result = SearchFilesTool(tmp_path).execute(
        {"query": "needle", "path": path, "include_hidden": include_hidden}
    )

    assert result.error_code == "PROTECTED_FILE"
    assert "PRIVATE_VALUE" not in str(result)


def test_explicit_alias_to_custom_credentials_is_rejected(tmp_path, monkeypatch):
    protected = write_file(tmp_path, "config.txt", "needle=PRIVATE_VALUE\n")
    (tmp_path / "alias.txt").symlink_to(protected)
    monkeypatch.setenv("AGENT_ENV_FILE", str(protected))

    result = SearchFilesTool(tmp_path).execute({"query": "needle", "path": "alias.txt"})

    assert result.error_code == "PROTECTED_FILE"
    assert "PRIVATE_VALUE" not in str(result)


@pytest.mark.parametrize("include_hidden", [None, False, True])
def test_hidden_default_matches_schema_and_filters_directories(tmp_path, include_hidden):
    for name in ["visible.txt", ".hidden.txt", ".hidden/nested.txt"]:
        write_file(tmp_path, name)
    tool = SearchFilesTool(tmp_path)
    assert tool.definition.parameters["properties"]["include_hidden"]["default"] is False
    args = {"query": "needle"}
    if include_hidden is not None:
        args["include_hidden"] = include_hidden

    result = tool.execute(args)

    assert result.success
    expected = {"visible.txt"}
    if include_hidden:
        expected.update({".hidden.txt", ".hidden/nested.txt"})
    assert {match["path"] for match in result.data["matches"]} == expected


@pytest.mark.parametrize("path", [".hidden.txt", ".hidden", ".hidden/nested.txt"])
@pytest.mark.parametrize("include_hidden", [False, True])
def test_explicit_hidden_paths_follow_include_hidden(tmp_path, path, include_hidden):
    write_file(tmp_path, ".hidden.txt")
    write_file(tmp_path, ".hidden/nested.txt")

    result = SearchFilesTool(tmp_path).execute(
        {"query": "needle", "path": path, "include_hidden": include_hidden}
    )

    assert result.success
    assert result.data["matches_returned"] == (1 if include_hidden else 0)


@pytest.mark.parametrize("path", [".", "tests", "tests/test_a.py"])
@pytest.mark.parametrize("pattern", ["*.py", "tests/test_*.py"])
def test_glob_uses_workspace_relative_path_for_files_and_directories(tmp_path, path, pattern):
    write_file(tmp_path, "tests/test_a.py")
    write_file(tmp_path, "tests/notes.txt")
    write_file(tmp_path, "docs/notes.txt")

    result = SearchFilesTool(tmp_path).execute({"query": "needle", "path": path, "glob": pattern})

    assert result.success
    assert result.data["matches"] == [
        {"path": "tests/test_a.py", "line_number": 1, "line": "needle"}
    ]


def test_glob_does_not_match_same_filename_in_different_directory(tmp_path):
    write_file(tmp_path, "tests/test_a.py")
    write_file(tmp_path, "other/test_a.py")

    result = SearchFilesTool(tmp_path).execute({"query": "needle", "glob": "tests/test_*.py"})

    assert result.success
    assert [match["path"] for match in result.data["matches"]] == ["tests/test_a.py"]


def test_search_rejects_outside_workspace_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_file(tmp_path, "outside.txt")

    result = SearchFilesTool(workspace).execute({"query": "needle", "path": "../outside.txt"})

    assert result.error_code == "PATH_OUTSIDE_WORKSPACE"
