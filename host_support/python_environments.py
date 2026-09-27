"""Select an interpreter without executing project-controlled code or granting access."""

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import environment_python


@dataclass(frozen=True)
class PythonSelection:
    executable: Path
    source: str


def discover_python(workspace, explicit=None, *, environment=None, agent_python=None):
    env = os.environ if environment is None else environment
    agent = Path(agent_python or sys.executable).absolute()
    workspace = Path(workspace).resolve()
    selected, source = None, None
    if explicit or env.get("AGENT_PROJECT_PYTHON"):
        value = explicit or env["AGENT_PROJECT_PYTHON"]
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        selected, source = candidate.absolute(), "explicit"
    else:
        candidates = []
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX"):
            if env.get(key):
                candidates.append(
                    (
                        environment_python(
                            Path(env[key]).expanduser(),
                            kind="conda" if key == "CONDA_PREFIX" else "venv",
                        ),
                        key,
                    )
                )
        candidates.append((environment_python(workspace / ".venv"), "workspace .venv"))
        for name in ("python", "python3"):
            found = shutil.which(name, path=env.get("PATH", ""))
            if found:
                candidates.append((Path(found), "launch PATH"))
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
            return PythonSelection(agent, "agent fallback")
    if not selected.is_file() or not os.access(selected, os.X_OK):
        raise ValueError(f"项目 Python 不可执行：{selected}")
    return PythonSelection(selected, source)
