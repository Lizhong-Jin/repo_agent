"""Opt-in end-to-end checks through the actual sandbox worker and Docker policy."""

import os
import shutil

import pytest

from sandbox import SandboxPolicy, SandboxSession
from sandbox.lsp_smoke import SOURCES


@pytest.mark.skipif(
    os.getenv("RUN_SANDBOX_DOCKER_TESTS") != "1",
    reason="Requires Docker and a built multilingual sandbox image",
)
def test_all_languages_through_sandbox_worker(tmp_path):
    expected = {
        "py": ("python", "pylsp"),
        "js": ("javascript", "typescript-language-server"),
        "jsx": ("javascriptreact", "typescript-language-server"),
        "ts": ("typescript", "typescript-language-server"),
        "tsx": ("typescriptreact", "typescript-language-server"),
        "go": ("go", "gopls"),
        "c": ("c", "clangd"),
        "cpp": ("cpp", "clangd"),
    }
    (tmp_path / "go.mod").write_text("module example.com/smoke\n\ngo 1.25\n")
    for filename, content in SOURCES.items():
        extension = filename.rsplit(".", 1)[1]
        directory = tmp_path / extension
        directory.mkdir()
        (directory / filename).write_text(content)
    policy = SandboxPolicy(image=os.getenv("SANDBOX_LSP_TEST_IMAGE", "repo-agent-sandbox:v1"))
    session = SandboxSession(tmp_path, policy=policy)
    try:
        tool = next(t for t in session.tools() if t.definition.name == "get_symbols")
        for filename in SOURCES:
            extension = filename.rsplit(".", 1)[1]
            result = tool.execute({"path": f"{extension}/{filename}"})
            assert result.success, (filename, result)
            assert (result.data["language_id"], result.data["server_id"]) == expected[extension]
            assert any(s["name"].lower() == "example" for s in result.data["symbols"])
        # Listing symbols must not leave language-server caches in the workspace.
        assert session.changes()[1] == []
    finally:
        shutil.rmtree(session.directory)
