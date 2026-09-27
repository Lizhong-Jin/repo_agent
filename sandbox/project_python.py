"""Select project Python without executing project-controlled code on the host."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPython:
    executable: Path
    source: str
    read_paths: tuple[Path, ...]


def select_python(workspace, explicit=None, *, environment=None, agent_python=None,
                  trusted_paths=()):
    env = os.environ if environment is None else environment
    agent = Path(agent_python or sys.executable).absolute()
    workspace = Path(workspace).resolve()
    selected, source = None, None
    if explicit or env.get('AGENT_PROJECT_PYTHON'):
        value = explicit or env['AGENT_PROJECT_PYTHON']
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        selected, source = candidate.absolute(), 'explicit'
    else:
        candidates = []
        for key in ('VIRTUAL_ENV', 'CONDA_PREFIX'):
            if env.get(key):
                candidates.append((Path(env[key]).expanduser() / 'bin/python', key))
        candidates.append((workspace / '.venv/bin/python', 'workspace .venv'))
        for name in ('python', 'python3'):
            found = shutil.which(name, path=env.get('PATH', ''))
            if found:
                candidates.append((Path(found), 'launch PATH'))
        for path, origin in candidates:
            path = path.absolute()
            # Preserve the venv entry path: resolving its python symlink would
            # silently switch to the base interpreter and lose project packages.
            if path == agent or path.parent == agent.parent:
                continue
            if path.is_file() and os.access(path, os.X_OK):
                selected, source = path, origin
                break
        if selected is None:
            return ProjectPython(agent, 'agent fallback', ())
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise ValueError(f'项目 Python 不可执行：{selected}')
    resolved = selected.resolve(strict=True)
    config = selected.parent.parent / 'pyvenv.cfg'
    roots = {resolved.parent.parent}
    if config.is_file() or not selected.is_symlink():
        roots.add(selected.parent.parent.resolve())
    else:
        roots.add(selected.parent.resolve())
    if config.is_file():
        for line in config.read_text().splitlines():
            key, _, value = line.partition('=')
            if key.strip() == 'home':
                base = Path(value.strip())
                if not base.is_absolute():
                    raise ValueError('pyvenv.cfg home 必须为绝对路径')
                base = base.resolve(strict=True).parent
                if not resolved.is_relative_to(base) and not any(
                    base.is_relative_to(path.resolve()) for path in trusted_paths
                ):
                    raise ValueError('pyvenv.cfg 不能授权无关宿主目录；'
                                     '请使用符号链接式 venv 或 Conda 环境')
                roots.add(base)
    # Only conventional bin/python layouts are granted. Never grant whole home
    # or filesystem roots to accommodate a custom shell wrapper.
    if selected.parent.name != 'bin' or any(
        path == workspace or path in {Path('/'), Path('/home'), Path('/Users'), Path.home()}
        for path in roots
    ):
        raise ValueError('项目 Python 必须使用常规环境的 bin/python 路径')
    return ProjectPython(selected, source, tuple(sorted(roots)))
