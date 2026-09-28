from pathlib import Path

import pytest

from tools.filesystem import FindFileTool


@pytest.mark.parametrize("include_hidden", [False, True])
def test_credentials_are_excluded_before_counts_and_limit(tmp_path, monkeypatch, include_hidden):
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "config.txt"))
    for name in (".env", ".ENV.production", "config.txt", "visible.txt"):
        (tmp_path / name).write_text("fixture")
    (tmp_path / ".env.local").mkdir()
    (tmp_path / ".env.local" / "settings.txt").write_text("fixture")
    (tmp_path / "alias").symlink_to("config.txt")
    (tmp_path / "directory-alias").symlink_to(".env.local", target_is_directory=True)

    result = FindFileTool(tmp_path, max_results=1).execute(
        {
            "pattern": "**/*",
            "include_hidden": include_hidden,
        }
    )

    assert result.success
    assert [item["path"] for item in result.data["matches"]] == ["visible.txt"]
    assert result.data["total_matches"] == 1
    assert result.data["returned_matches"] == 1
    assert result.data["truncated"] is False


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        ".env.local/nested",
        "public-alias",
        ".env.alias",
        "config.txt",
    ],
)
def test_protected_search_root_is_rejected_before_glob(tmp_path, monkeypatch, path):
    (tmp_path / ".env").write_text("fixture")
    (tmp_path / ".env.local" / "nested").mkdir(parents=True)
    (tmp_path / "public").mkdir()
    (tmp_path / "public-alias").symlink_to(".env.local", target_is_directory=True)
    (tmp_path / ".env.alias").symlink_to("public", target_is_directory=True)
    (tmp_path / "config.txt").write_text("fixture")
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "config.txt"))

    def unexpected_glob(*args, **kwargs):
        pytest.fail("Protected search roots must not be traversed")

    monkeypatch.setattr(Path, "glob", unexpected_glob)
    result = FindFileTool(tmp_path).execute({"path": path, "pattern": "**/*"})
    assert result.error_code == "PROTECTED_FILE"


@pytest.mark.parametrize(
    "pattern",
    [
        ".env.local/*",
        "public-alias/*",
        ".env.alias/*",
        "config.txt",
        "file-alias",
    ],
)
def test_literal_globs_cannot_expose_credentials_or_aliases(tmp_path, monkeypatch, pattern):
    (tmp_path / ".env.local").mkdir()
    (tmp_path / ".env.local" / "settings.txt").write_text("fixture")
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "ordinary.txt").write_text("fixture")
    (tmp_path / "public-alias").symlink_to(".env.local", target_is_directory=True)
    (tmp_path / ".env.alias").symlink_to("public", target_is_directory=True)
    (tmp_path / "config.txt").write_text("fixture")
    (tmp_path / "file-alias").symlink_to("config.txt")
    monkeypatch.setenv("AGENT_ENV_FILE", str(tmp_path / "config.txt"))

    result = FindFileTool(tmp_path).execute({"pattern": pattern})
    assert result.success
    assert result.data["matches"] == []
    assert result.data["total_matches"] == 0
    assert result.data["returned_matches"] == 0
    assert result.data["truncated"] is False


@pytest.mark.parametrize(
    "pattern",
    [
        "..",
        "../*.py",
        "sub/../*.py",
        "**/../*.py",
        "./../*",
        "sub//..//file.txt",
    ],
)
def test_parent_segments_are_rejected_before_glob(tmp_path, monkeypatch, pattern):
    def unexpected_glob(*args, **kwargs):
        pytest.fail("Invalid patterns must not be traversed")

    monkeypatch.setattr(Path, "glob", unexpected_glob)
    result = FindFileTool(tmp_path).execute({"pattern": pattern})
    assert result.error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("inside_workspace", [False, True])
def test_absolute_patterns_are_rejected_before_glob(tmp_path, monkeypatch, inside_workspace):
    absolute = (tmp_path if inside_workspace else tmp_path.parent) / "*.py"

    def unexpected_glob(*args, **kwargs):
        pytest.fail("Absolute patterns must not be traversed")

    monkeypatch.setattr(Path, "glob", unexpected_glob)
    result = FindFileTool(tmp_path).execute({"pattern": str(absolute)})
    assert result.error_code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("**/*.py", {"src/main.py", "src/pkg/test_unit.py"}),
        ("pkg/**/test_*.py", {"src/pkg/test_unit.py"}),
        ("./main.py", {"src/main.py"}),
        ("file..txt", {"src/file..txt"}),
    ],
)
def test_valid_patterns_remain_relative_to_search_directory(tmp_path, pattern, expected):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    for name in ("src/main.py", "src/pkg/test_unit.py", "src/file..txt", "outside.py"):
        (tmp_path / name).write_text("fixture")

    result = FindFileTool(tmp_path).execute({"path": "src", "pattern": pattern, "type": "file"})
    assert result.success
    assert {item["path"] for item in result.data["matches"]} == expected
