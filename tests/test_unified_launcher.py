"""Portable launcher routing and Linux shell-argument preservation."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "run_agent.sh"


@pytest.fixture
def launcher(tmp_path):
    install = tmp_path / "agent install"
    project = tmp_path / "my project"
    bins = install / ".venv" / "bin"
    bins.mkdir(parents=True)
    project.mkdir()
    shutil.copy2(SOURCE, install / "run_agent.sh")
    (bins / "activate").write_text("")
    (bins / "python").write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "print(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd()},ensure_ascii=False))\n"
        "sys.exit(7 if '--fail' in sys.argv else 0)\n"
    )
    (bins / "python").chmod(0o755)
    env = dict(os.environ)
    env.pop("AGENT_ENV_FILE", None)
    env.pop("AGENT_LOG_DIR", None)
    env["PATH"] = str(bins) + os.pathsep + env["PATH"]
    return install, project, bins, env


def test_build_entry_does_not_require_model_or_load_project_env(launcher):
    install, project, _, env = launcher
    (project / ".env").write_text("deliberately invalid model configuration")
    result = subprocess.run(
        [str(install / "run_agent.sh"), "--build-sandbox"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["-m", "sandbox.build"]
    assert data["cwd"] == str(install)


def test_linux_launcher_preserves_literals_and_exit_code(launcher, tmp_path):
    install, project, bins, env = launcher
    (bins / "uname").write_text("#!/bin/sh\nprintf 'Linux\\n'\n")
    (bins / "uname").chmod(0o755)
    (bins / "script").write_text(
        f"#!{sys.executable}\nimport os,subprocess,sys\n"
        "assert os.environ['SHELL']=='/bin/bash'\n"
        "command=sys.argv[sys.argv.index('-c')+1]\n"
        "sys.exit(subprocess.run(['/bin/bash','-c',command]).returncode)\n"
    )
    (bins / "script").chmod(0o755)
    marker = tmp_path / "must not exist"
    task = f"写算子; touch '{marker}' $(echo injected)\n第二行"
    result = subprocess.run(
        [str(install / "run_agent.sh"), task, "--fail"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["-u", "-m", "cli.main", task, "--fail", "--root", str(project)]
    assert not marker.exists()
