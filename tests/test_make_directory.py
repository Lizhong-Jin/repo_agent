import pytest

from tools.filesystem import MakeDirectoryTool


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", ".env.example", ".ENV.LOCAL", "sub/.env", ".env.local/sub"],
)
@pytest.mark.parametrize("parents", [False, True])
def test_protected_path_rejected_before_creating_any_directory(tmp_path, name, parents):
    result = MakeDirectoryTool(tmp_path).execute({"path": name, "parents": parents})

    assert not result.success
    assert result.error_code == "PROTECTED_FILE"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.local/sub"])
def test_existing_protected_directory_is_not_reported_as_success(tmp_path, name):
    target = tmp_path / name
    target.mkdir(parents=True)
    marker = target / "marker.txt"
    marker.write_text("private content")

    result = MakeDirectoryTool(tmp_path).execute({"path": name})

    assert not result.success
    assert result.error_code == "PROTECTED_FILE"
    assert marker.read_text() == "private content"
    assert "private content" not in str(result)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("child", [False, True])
def test_protected_symlink_spelling_and_destination_are_checked(tmp_path, reverse, child):
    target = tmp_path / ("ordinary" if reverse else ".env.local")
    alias = tmp_path / (".env.local" if reverse else "alias")
    target.mkdir()
    alias.symlink_to(target, target_is_directory=True)
    path = alias / "child" if child else alias

    result = MakeDirectoryTool(tmp_path).execute({"path": str(path), "parents": True})

    assert not result.success
    assert result.error_code == "PROTECTED_FILE"
    assert alias.is_symlink()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("existing", ["missing", "file", "directory"])
def test_custom_credential_path_is_protected(tmp_path, monkeypatch, existing):
    target = tmp_path / "config.txt"
    if existing == "file":
        target.write_text("private content")
    elif existing == "directory":
        target.mkdir()
    monkeypatch.setenv("AGENT_ENV_FILE", str(target))

    result = MakeDirectoryTool(tmp_path).execute({"path": target.name, "parents": True})

    assert not result.success
    assert result.error_code == "PROTECTED_FILE"
    assert "private content" not in str(result)
    if existing == "missing":
        assert not target.exists()
    elif existing == "file":
        assert target.read_text() == "private content"
    else:
        assert target.is_dir()


@pytest.mark.parametrize("name", ["ordinary", ".config", "env", ".environment"])
def test_unprotected_directory_can_be_created_and_reused(tmp_path, name):
    tool = MakeDirectoryTool(tmp_path)

    created = tool.execute({"path": name})
    reused = tool.execute({"path": name})

    assert created.success
    assert created.data == {"path": name, "created": True}
    assert reused.success
    assert reused.data == {"path": name, "created": False}
    assert (tmp_path / name).is_dir()


def test_unprotected_final_symlink_is_still_rejected(tmp_path):
    target = tmp_path / "ordinary"
    target.mkdir()
    (tmp_path / "alias").symlink_to(target, target_is_directory=True)

    result = MakeDirectoryTool(tmp_path).execute({"path": "alias"})

    assert result.error_code == "PATH_IS_SYMLINK"
