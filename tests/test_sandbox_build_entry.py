"""Exercise the installed build entry without a model configuration or real Docker."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize(
    "flags,image",
    [
        ([], "repo-agent-sandbox:v1"),
        (["--profile", "standard", "--image", "custom-sandbox:test"], "custom-sandbox:test"),
    ],
)
def test_build_entry_uses_installation_from_an_unrelated_directory(
    tmp_path, flags, image, exit_code
):
    workspace = tmp_path / "unrelated project"
    workspace.mkdir()
    (workspace / ".env").write_text("deliberately invalid model configuration")
    user_config = tmp_path / "user config"
    user_config.mkdir()
    (user_config / ".env").write_text("also deliberately invalid")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1] == 'info':\n"
        "    print(json.dumps({'OSType': 'linux', 'Architecture': 'x86_64', "
        "'Runtimes': {'runc': {}}}))\n"
        "elif sys.argv[1] == 'build':\n"
        "    Path(os.environ['BUILD_RECORD']).write_text(json.dumps(sys.argv[1:]))\n"
        "    sys.exit(int(os.environ['BUILD_EXIT_CODE']))\n"
        "else:\n"
        "    sys.exit(99)\n"
    )
    docker.chmod(0o755)
    record = tmp_path / "build.json"
    result = subprocess.run(
        [str(SOURCE / ".venv/bin/repo-agent-build-sandbox"), *flags],
        cwd=workspace,
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
            "AGENT_CONFIG_DIR": str(user_config),
            "AGENT_ENV_FILE": str(workspace / ".env"),
            "BUILD_RECORD": str(record),
            "BUILD_EXIT_CODE": str(exit_code),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    args = json.loads(record.read_text())
    assert args[args.index("-f") + 1] == str(SOURCE / "sandbox/Dockerfile")
    assert args[args.index("-t") + 1] == image
    assert args[-1] == str(SOURCE)
    assert "SANDBOX_PROFILE=standard" in args
    assert set(path.name for path in workspace.iterdir()) == {".env"}


def test_build_help_needs_neither_docker_nor_model_config(tmp_path):
    result = subprocess.run(
        [str(SOURCE / ".venv/bin/repo-agent-build-sandbox"), "--help"],
        cwd=tmp_path,
        env={**os.environ, "PATH": "", "AGENT_ENV_FILE": str(tmp_path / "missing")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--profile" in result.stdout and "--image" in result.stdout
