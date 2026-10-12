"""Upgrade between actual release archives, preserving configuration and old installation."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from packaging.version import Version
from release_helpers import ROOT, offline_prefix

from configuration.literal import parse_config
from installer.paths import extract_files
from installer.release_install import locate_release_root
from installer.release_manifest import read_release


@pytest.mark.skipif(
    not os.getenv("REPO_AGENT_PREVIOUS_ARCHIVE"),
    reason="Requires previous and candidate release archives",
)
def test_real_previous_release_upgrade(tmp_path):
    candidate = os.environ.get(
        "REPO_AGENT_WINDOWS_ARCHIVE" if os.name == "nt" else "REPO_AGENT_TEST_ARCHIVE"
    )
    assert candidate, "Missing candidate archive"
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AGENT_", "LLM_", "XDG_", "PYTHON")) and not k.endswith("_API_KEY")
    }
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        PYTHONUTF8="1",
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_STATE_HOME=str(tmp_path / "state"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        AGENT_PYTHON_CACHE=str(tmp_path / "runtime"),
    )
    (tmp_path / "home").mkdir()
    bins = tmp_path / "commands"
    project = tmp_path / "project"
    project.mkdir()
    prefix = [] if os.name == "nt" else offline_prefix()

    def run(command, *, input=""):
        result = subprocess.run(
            [*prefix, *map(str, command)],
            env=env,
            cwd=project,
            input=input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    roots = []
    versions = []
    for stage, source in enumerate((os.environ["REPO_AGENT_PREVIOUS_ARCHIVE"], candidate)):
        download = tmp_path / f"download-{stage}"
        download.mkdir()
        extract_files(Path(source), download)
        bundle = locate_release_root(download)
        manifest = read_release(bundle)
        versions.append(manifest["version"])
        if os.name == "nt":
            command = [
                shutil.which("powershell.exe"),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                bundle / "install_release.ps1",
            ]
        else:
            env["AGENT_PYTHON_ARCHIVE"] = str(bundle / "runtime/python.tar.gz")
            command = ["/bin/bash", bundle / "install-release.sh"]
        run([*command, "--mode", "local", "--offline", "--no-path", "--bin-dir", bins], input="y\n")
        root = tmp_path / "data/repo-agent/versions" / manifest["version"]
        roots.append(root)
        public = bins / ("repo-agent.exe" if os.name == "nt" else "repo-agent")
        info = json.loads(run([public, "version"]))
        assert info["installation"] == str(root)
        config = tmp_path / "config/repo-agent/.env"
        if stage == 0:
            run([public, "config", "set", "LLM_MODEL", "upgrade-keep"])
            before = config.read_bytes()
            python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            run([python, "-I", ROOT / "scripts/upgrade_session.py", "seed", project])
        else:
            assert (
                parse_config(config.read_text(encoding="utf-8"), source=config)["LLM_MODEL"]
                == "upgrade-keep"
            )
            python = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
            run([python, "-I", ROOT / "scripts/upgrade_session.py", "check", project])
            assert roots[0].is_dir() and (roots[0] / ".venv").is_dir()
            assert before  # An actual populated previous configuration was used.
        shutil.rmtree(download)
        run([public, "doctor", "--mode", "local"])
    assert Version(versions[0]) < Version(versions[1]), "Upgrade baseline must be older"
