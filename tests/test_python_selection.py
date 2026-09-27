"""Exercise install.sh interpreter discovery without performing an installation."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "source copy"
    (root / "cli").mkdir(parents=True)
    shutil.copy2(SOURCE / "install.sh", root / "install.sh")
    shutil.copy2(SOURCE / "cli/maintenance.py", root / "cli/maintenance.py")
    (root / "cli/setup.py").write_text(
        "import os,sys,json; print('SETUP:' + json.dumps(sys.argv[1:])); open(os.environ['SELECTION_RESULT'], 'w').write(os.environ.get('SELECTED_PYTHON', ''))"
    )
    return root


def candidate(tmp_path, name, state):
    directory = tmp_path / name
    directory.mkdir()
    path = directory / "python3"
    # Fake interpreters report component status, but run the actual setup stub with Python.
    script = "#!/bin/bash\n"
    script += 'if [[ "$1" == -I && "$4" == *"missing = []"* ]]; then\n'
    script += '  if [[ "$5" == 1 ]]; then exit 0; fi\n'
    script += {
        "full": "exit 0",
        "partial": "printf '缺少或不可用：ensurepip'; exit 2",
        "old": "printf '版本低于 Python 3.11'; exit 1",
    }[state] + "\nfi\n"
    script += "export SELECTED_PYTHON=" + shlex.quote(str(path)) + "\n"
    script += "exec " + shlex.quote(sys.executable) + ' "$@"\n'
    path.write_text(script)
    path.chmod(0o700)
    return path


def launch(root, tmp_path, paths, explicit=None, args=()):
    env = {
        **os.environ,
        "PATH": ":".join(str(path.parent) for path in paths),
        "SELECTION_RESULT": str(tmp_path / "chosen"),
    }
    env["AGENT_PYTHON"] = "system"
    if explicit is not None:
        env["AGENT_PYTHON"] = str(explicit)
    # install.sh uses dirname before discovering Python; provide only that utility.
    utils = tmp_path / "utils"
    utils.mkdir(exist_ok=True)
    (utils / "dirname").symlink_to("/usr/bin/dirname")
    env["PATH"] += ":" + str(utils)
    return subprocess.run(
        ["/bin/bash", str(root / "install.sh"), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_skips_incomplete_python_and_uses_later_path_entry(checkout, tmp_path):
    broken = candidate(tmp_path, "first", "partial")
    full = candidate(tmp_path, "second with spaces", "full")
    result = launch(checkout, tmp_path, [broken, full], args=("--check", "--mode", "local"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "chosen").read_text() == str(full)
    assert "ensurepip" in result.stderr
    assert "--check" in result.stdout


def test_explicit_incomplete_python_is_never_replaced(checkout, tmp_path):
    broken = candidate(tmp_path, "explicit", "partial")
    full = candidate(tmp_path, "other", "full")
    result = launch(checkout, tmp_path, [full], explicit=broken)
    assert result.returncode == 1
    assert not (tmp_path / "chosen").exists()
    assert "未找到组件齐全" in result.stderr


def test_explicit_missing_python_does_not_fall_back(checkout, tmp_path):
    full = candidate(tmp_path, "other", "full")
    result = launch(checkout, tmp_path, [full], explicit=tmp_path / "absent")
    assert result.returncode == 1
    assert "解释器不存在" in result.stderr
    assert not (tmp_path / "chosen").exists()


def test_recovery_works_without_venv_components(checkout, tmp_path):
    broken = candidate(tmp_path, "old installation", "partial")
    result = launch(checkout, tmp_path, [broken], explicit=broken, args=("--recover",))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "chosen").read_text() == str(broken)
    assert "--recover" in result.stdout


def test_versioned_python_and_arguments_are_preserved(checkout, tmp_path):
    old = candidate(tmp_path, "old", "old")
    full = candidate(tmp_path, "versioned", "full")
    renamed = full.with_name("python3.12")
    full.rename(renamed)
    result = launch(
        checkout,
        tmp_path,
        [old, renamed],
        args=("--bin-dir", str(tmp_path / "bin space"), "--skip-toolchains"),
    )
    assert result.returncode == 0, result.stderr
    arguments = json.loads(
        next(
            line[len("SETUP:") :]
            for line in result.stdout.splitlines()
            if line.startswith("SETUP:")
        )
    )
    assert arguments[-3:] == ["--bin-dir", str(tmp_path / "bin space"), "--skip-toolchains"]
