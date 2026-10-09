"""Windows Python layout validation and private relocation; no host-side probes."""

import os
from dataclasses import dataclass
from pathlib import Path

from host_support.filesystem import open_file
from host_support.python_environments import discover_python

from .project_python import ProjectPython
from .windows_workspace import copy_private_tree


@dataclass(frozen=True)
class WindowsPythonLayout:
    executable: Path
    root: Path
    base: Path
    source: str

    @property
    def read_paths(self):
        return tuple(dict.fromkeys((self.root, self.base)))


def _config(path):
    with os.fdopen(open_file(path), "r", encoding="utf-8-sig") as stream:
        text = stream.read(16385)
    if len(text) > 16384:
        raise ValueError("Oversized pyvenv.cfg")
    return {
        key.strip().lower(): value.strip()
        for line in text.splitlines()
        if (key := line.partition("=")[0]) and (value := line.partition("=")[2])
    }


def inspect_python(executable, source, workspace, *, trusted_base=None):
    executable = Path(executable).absolute()
    if executable.name.lower() != "python.exe":
        raise ValueError("Windows native requires a conventional python.exe interpreter")
    root = executable.parent
    venv = root.name.lower() == "scripts"
    if venv:
        root = root.parent
        configuration = _config(root / "pyvenv.cfg")
        base = Path(configuration.get("home", ""))
        if not base.is_absolute():
            raise ValueError("Windows venv home must be an absolute Python installation path")
    else:
        base = root
    blocked = {
        Path.home(),
        Path(workspace),
        Path(workspace).parent,
        Path(os.environ.get("SystemRoot", "C:/Windows")),
    }
    for directory in (root, base):
        if directory in blocked or directory == Path(directory.anchor):
            raise ValueError("Refusing an overly broad Python environment root")
        if directory.resolve(strict=True) != directory.absolute():
            raise ValueError("Python environments cannot traverse reparse points or links")
    # Read the selected entry via the no-follow service; never execute it here.
    with os.fdopen(open_file(executable), "rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Windows Python must be a native executable")
    if not (base / "python.exe").is_file() or not (base / "Lib/encodings").is_dir():
        raise ValueError("Windows Python needs a complete standard installation (Lib/encodings)")
    if venv and trusted_base is not None and base.resolve() != Path(trusted_base).resolve():
        raise ValueError("Agent venv points to an unexpected base interpreter")
    return WindowsPythonLayout(executable, root, base, source)


def select_project(workspace, explicit, agent_python, *, isolated_workspace=False):
    chosen = discover_python(
        workspace,
        explicit,
        agent_python=agent_python,
        environment={"PATH": os.devnull} if isolated_workspace else None,
    )
    layout = inspect_python(chosen.executable, chosen.source, workspace)
    return ProjectPython(chosen.executable, chosen.source, layout.read_paths), layout


def stage_python(layout, directory):
    directory = Path(directory)
    root = directory / "environment"

    # Grant only conventional runtime contents, not arbitrary siblings of Python.
    def allowed(relative):
        first = relative.split("/", 1)[0].lower()
        return (
            first in {"lib", "dlls", "scripts", "library", "include", "libs", "tcl"}
            or first == "pyvenv.cfg"
            or (
                "/" not in relative
                and (
                    first.endswith(".dll")
                    or first.startswith("python")
                    and first.endswith((".exe", ".zip", "._pth"))
                )
            )
        )

    copy_private_tree(layout.root, root, allowed=allowed)
    if layout.base != layout.root:
        base = directory / "base"
        copy_private_tree(layout.base, base, allowed=allowed)
        (root / "pyvenv.cfg").write_text(
            f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8"
        )
    else:
        base = root
    executable = root / layout.executable.relative_to(layout.root)
    return executable, base
