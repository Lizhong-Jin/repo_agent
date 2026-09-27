"""Project interpreter selection and isolation of control-process environments."""

import json
import os
import sys

import pytest
import test_native_performance_metrics as metrics_tests
from test_native_performance_metrics import outcome

from sandbox.project_python import select_python
from tools._internal.process_runner import ProcessRunner

backend = metrics_tests.backend


def executable(root):
    path = root / 'bin/python'
    path.parent.mkdir(parents=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


def test_selection_precedence_and_agent_exclusion(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    local = executable(workspace / '.venv')
    active = executable(tmp_path / 'active')
    explicit = executable(tmp_path / 'explicit')
    agent = executable(tmp_path / 'agent')
    env = {'VIRTUAL_ENV': str(active.parent.parent), 'PATH': str(agent.parent)}
    assert select_python(workspace, explicit, environment=env).executable == explicit
    assert select_python(workspace, environment=env).executable == active
    env['VIRTUAL_ENV'] = str(agent.parent.parent)
    assert select_python(workspace, environment=env, agent_python=agent).executable == local
    local.unlink()
    chosen = select_python(workspace, environment=env, agent_python=agent)
    assert chosen.executable == agent and chosen.source == 'agent fallback'


def test_venv_entry_is_preserved_and_base_runtime_is_mounted(tmp_path):
    base = executable(tmp_path / 'base')
    venv = tmp_path / 'project/.venv'
    (venv / 'bin').mkdir(parents=True)
    python = venv / 'bin/python'
    python.symlink_to(base)
    (venv / 'pyvenv.cfg').write_text(f'home = {base.parent}\n')
    chosen = select_python(venv.parent, python, environment={})
    assert chosen.executable == python
    assert set(chosen.read_paths) == {venv.resolve(), base.parent.parent.resolve()}


def test_explicit_missing_interpreter_never_falls_back(tmp_path):
    with pytest.raises(ValueError, match='不可执行'):
        select_python(tmp_path, 'missing', environment={})


def test_project_python_is_only_probed_through_sandbox(backend, monkeypatch, tmp_path):
    python = executable(tmp_path / 'project-env')
    backend.project_python = select_python(backend.workspace, python, environment={})
    calls = []

    def run(self, command, **kwargs):
        calls.append((command, self.base_env))
        assert command[0] == str(backend.executable)
        assert str(python) in command
        assert str(python.parent.parent) in command
        return outcome(json.dumps({'version': '3.12.0', 'executable': str(python)}))

    monkeypatch.setattr(ProcessRunner, 'run', run)
    info = backend._probe_project_python()
    assert info['version'] == '3.12.0'
    assert calls[0][1]['PATH'].split(os.pathsep)[1] == str(python.parent)


def test_project_mounts_and_path_are_excluded_from_control_commands(backend, monkeypatch, tmp_path):
    python = executable(tmp_path / 'project-env')
    backend.project_python = select_python(backend.workspace, python, environment={})
    calls = []

    def run(self, command, **kwargs):
        calls.append((command, self.base_env))
        return outcome()

    monkeypatch.setattr(ProcessRunner, 'run', run)
    backend._run(['/usr/bin/true'])
    backend._run(['/usr/bin/true'], project=True)
    backend._run(request={'name': 'get_symbols', 'arguments': {}}, project=True)
    assert str(python.parent.parent) not in calls[0][0]
    assert str(python.parent.parent) in calls[1][0]
    assert calls[1][1]['PATH'].split(os.pathsep)[1] == str(python.parent)
    assert calls[2][1]['PATH'].split(os.pathsep)[0] == str(backend.python.parent)


def test_workspace_pyvenv_config_cannot_grant_unrelated_host_directory(tmp_path):
    python = executable(tmp_path / 'workspace/.venv')
    unrelated = tmp_path / 'private/bin'
    unrelated.mkdir(parents=True)
    (python.parent.parent / 'pyvenv.cfg').write_text(f'home = {unrelated}\n')
    with pytest.raises(ValueError, match='不能授权'):
        select_python(tmp_path / 'workspace', python, environment={})


def test_lightweight_file_tools_keep_project_environment_readonly(backend):
    python = executable(backend.workspace / '.venv')
    backend.project_python = select_python(backend.workspace, python, environment={})
    result = backend.execute(backend.workspace, 'write_file', {
        'path': '.venv/lib/replacement.py', 'content': 'changed',
    })
    assert not result.success
    assert not (python.parent.parent / 'lib/replacement.py').exists()


@pytest.mark.skipif(
    sys.platform != 'darwin' or os.getenv('RUN_SANDBOX_NATIVE_TESTS') != '1',
    reason='Requires real macOS Seatbelt',
)
def test_real_project_venv_and_python_aliases_stay_isolated(tmp_path):
    import subprocess

    from sandbox.native import NativeBackend

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    prefix = workspace / '.venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(prefix)], check=True)
    site = prefix / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    (site / 'project_only_pkg.py').write_text('VALUE = "project-only"\n')
    outside = tmp_path / 'host-write-attempt'
    (site / 'sitecustomize.py').write_text(
        f'from pathlib import Path\ntry: Path({str(outside)!r}).write_text("unsafe")\n'
        'except OSError: pass\n')
    backend = NativeBackend(workspace, profile='standard', project_python=prefix / 'bin/python')
    try:
        assert not outside.exists()  # Even interpreter startup ran inside the OS sandbox.
        for tool, arguments in (
            ('run_python', {'code': 'import project_only_pkg; print(project_only_pkg.VALUE)'}),
            ('run_command', {'command': ['python', '-c',
                                        'import project_only_pkg; print(project_only_pkg.VALUE)']}),
            ('run_command', {'command': ['/bin/sh', '-c',
                                        'python3 -c "import project_only_pkg; '
                                        'print(project_only_pkg.VALUE)"']}),
        ):
            result = backend.execute(workspace, tool, arguments)
            assert result.success and result.data['exit_code'] == 0, result
            assert result.data['stdout'].strip() == 'project-only'
        assert not outside.exists()
        result = backend.execute(workspace, 'get_execution_environment', {'sections': ['runtimes']})
        assert result.data['runtimes']['python']['executable'] == str(prefix / 'bin/python')
        assert result.data['runtimes']['agent_python']['executable'] == str(backend.python)
    finally:
        backend.close()
