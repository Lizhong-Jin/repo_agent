import pytest

from tools import EditFileTool, ReadFileTool, WriteFileTool
from tools.filesystem import SearchFilesTool


@pytest.mark.parametrize("tool_type", [ReadFileTool, WriteFileTool, EditFileTool, SearchFilesTool])
@pytest.mark.parametrize("name", [".env", ".env.local", ".env.example", "sub/.env"])
def test_credentials_are_protected(tmp_path, tool_type, name):
    target = tmp_path / name
    target.parent.mkdir(exist_ok=True)
    target.write_text("secret")
    args = {"path": name}
    if tool_type is WriteFileTool:
        args.update(content="replacement", overwrite=True)
    elif tool_type is EditFileTool:
        args.update(edits=[{"old_text": "secret", "new_text": "replacement"}])
    elif tool_type is SearchFilesTool:
        args.update(query="secret", include_hidden=True)
    result = tool_type(tmp_path).execute({"reads": [args]} if tool_type is ReadFileTool else args)
    if tool_type is ReadFileTool:
        assert result.data["results"][0]["error"]["code"] == "PROTECTED_FILE"
    else:
        assert result.error_code == "PROTECTED_FILE"
    assert target.read_text() == "secret"
    assert "secret" not in str(result)


@pytest.mark.parametrize("tool_type", [ReadFileTool, WriteFileTool, EditFileTool, SearchFilesTool])
@pytest.mark.parametrize("reverse", [False, True])
def test_symlink_cannot_hide_credentials(tmp_path, tool_type, reverse):
    target = tmp_path / ("ordinary.txt" if reverse else ".env")
    alias = tmp_path / (".env" if reverse else "ordinary.txt")
    target.write_text("secret")
    alias.symlink_to(target)
    args = {"path": alias.name}
    if tool_type is WriteFileTool:
        args.update(content="replacement", overwrite=True)
    elif tool_type is EditFileTool:
        args.update(edits=[{"old_text": "secret", "new_text": "replacement"}])
    elif tool_type is SearchFilesTool:
        args.update(query="secret", include_hidden=True)
    result = tool_type(tmp_path).execute({"reads": [args]} if tool_type is ReadFileTool else args)
    if tool_type is ReadFileTool:
        assert result.data["results"][0]["error"]["code"] == "PROTECTED_FILE"
    else:
        assert result.error_code == "PROTECTED_FILE"
    assert target.read_text() == "secret"
    assert "secret" not in str(result)


@pytest.mark.parametrize("tool_type", [ReadFileTool, WriteFileTool, EditFileTool, SearchFilesTool])
def test_custom_env_is_protected(tmp_path, monkeypatch, tool_type):
    target = tmp_path / "config.txt"
    target.write_text("secret")
    monkeypatch.setenv("AGENT_ENV_FILE", str(target))
    args = {"path": target.name}
    if tool_type is WriteFileTool:
        args.update(content="replacement", overwrite=True)
    elif tool_type is EditFileTool:
        args.update(edits=[{"old_text": "secret", "new_text": "replacement"}])
    elif tool_type is SearchFilesTool:
        args.update(query="secret", include_hidden=True)
    result = tool_type(tmp_path).execute({"reads": [args]} if tool_type is ReadFileTool else args)
    if tool_type is ReadFileTool:
        assert result.data["results"][0]["error"]["code"] == "PROTECTED_FILE"
    else:
        assert result.error_code == "PROTECTED_FILE"
    assert target.read_text() == "secret"
    assert "secret" not in str(result)
