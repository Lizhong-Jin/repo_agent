"""Select project Python without executing project-controlled code on the host."""

from dataclasses import dataclass
from pathlib import Path

from host_support.python_environments import discover_python


@dataclass(frozen=True)
class ProjectPython:
    executable: Path
    source: str
    read_paths: tuple[Path, ...]


def select_python(
    workspace, explicit=None, *, environment=None, agent_python=None, trusted_paths=()
):
    workspace = Path(workspace).resolve()
    chosen = discover_python(
        workspace, explicit, environment=environment, agent_python=agent_python
    )
    if chosen.source == "agent fallback":
        return ProjectPython(chosen.executable, chosen.source, ())
    return ProjectPython(
        chosen.executable,
        chosen.source,
        authorized_read_paths(workspace, chosen.executable, trusted_paths),
    )


def authorized_read_paths(workspace, selected, trusted_paths=()):
    """POSIX sandbox grants; discovery itself never authorizes host directories."""
    resolved = selected.resolve(strict=True)
    config = selected.parent.parent / "pyvenv.cfg"
    roots = {resolved.parent.parent}
    if config.is_file() or not selected.is_symlink():
        roots.add(selected.parent.parent.resolve())
    else:
        roots.add(selected.parent.resolve())
    if config.is_file():
        for line in config.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                base = Path(value.strip())
                if not base.is_absolute():
                    raise ValueError("pyvenv.cfg home 必须为绝对路径")
                base = base.resolve(strict=True).parent
                if not resolved.is_relative_to(base) and not any(
                    base.is_relative_to(path.resolve()) for path in trusted_paths
                ):
                    raise ValueError(
                        "pyvenv.cfg 不能授权无关宿主目录；请使用符号链接式 venv 或 Conda 环境"
                    )
                roots.add(base)
    # Only conventional bin/python layouts are granted. Never grant whole home
    # or filesystem roots to accommodate a custom shell wrapper.
    if selected.parent.name != "bin" or any(
        path == workspace or path in {Path("/"), Path("/home"), Path("/Users"), Path.home()}
        for path in roots
    ):
        raise ValueError("项目 Python 必须使用常规环境的 bin/python 路径")
    return tuple(sorted(roots))
