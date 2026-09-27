"""Standard-library maintenance checks and cooperative process locks (macOS/Linux)."""

import importlib
import json
import os
import platform
import re
import shlex
import ssl
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()

from host_support.diagnostics import print_diagnostics
from host_support.filesystem import open_file
from host_support.locking import lock_descriptor
from host_support.paths import environment_python, scripts_dir


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = open_file(path, os.O_RDWR | os.O_CREAT, nonblocking=False)
    try:
        try:
            lock_descriptor(fd, blocking=False)
        except BlockingIOError:
            raise ValueError(f"另一个安装、卸载或配置操作正在进行，请稍后重试：{path}") from None
        yield
    finally:
        os.close(fd)  # Keep the inode: unlinking a lock file can split concurrent locks.


def probe(command, *, timeout=15, cwd=None):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def python_component_guidance():
    """Suggest repairs for the selected interpreter, without running package managers."""
    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    prefix = Path(sys.base_prefix)
    executable = str(getattr(sys, "_base_executable", None) or sys.executable)
    retry_python = shlex.quote(executable)
    instructions = [
        f"当前解释器：{sys.executable}；基础 Python：{prefix}",
        "venv/ensurepip 由 Python 发行版提供，不能通过 pip install venv/ensurepip 补齐。",
    ]
    if (prefix / "conda-meta").is_dir():
        instructions += [
            "检测到 Conda/Anaconda；修复这个环境中的 Python（不要装到其他 Conda 环境）：",
            f"  conda install --prefix {shlex.quote(str(prefix))} --force-reinstall python={version}",
        ]
    else:
        brew_python = re.search(r"/(?:Cellar|opt)/(python(?:@3\.\d+)?)/", str(prefix) + "/")
        if brew_python:
            formula = brew_python.group(1)
            instructions += [
                "检测到 Homebrew Python；重装其标准库组件：",
                f"  brew reinstall {formula}",
            ]
            # A Homebrew reinstall can change the versioned Cellar directory.
            retry_python = f'"$(brew --prefix {formula})/libexec/bin/python3"'
        elif sys.platform == "darwin":
            instructions += [
                "macOS：可安装完整 Homebrew Python（已有 Homebrew 时执行）：",
                "  brew install python",
                "没有 Homebrew 时，可从 https://www.python.org/downloads/macos/ 安装完整 Python 3.11+，",
                "然后通过 AGENT_PYTHON 指定新解释器的绝对路径。",
            ]
            retry_python = '"$(brew --prefix python)/libexec/bin/python3"'
        elif sys.platform == "linux" and str(prefix) == "/usr":
            try:
                release = platform.freedesktop_os_release()
            except OSError:
                release = {}
            family = {release.get("ID", ""), *release.get("ID_LIKE", "").split()}
            if family & {"debian", "ubuntu"}:
                instructions += [
                    "Debian/Ubuntu：安装与当前解释器次版本一致的 venv 包：",
                    "  sudo apt-get update",
                    f"  sudo apt-get install python{version}-venv",
                    "若找不到该包，请检查提供此 Python 版本的软件源；不要安装其他版本的 venv 包替代。",
                ]
            elif family & {"fedora", "rhel", "centos"}:
                instructions += [
                    "Fedora/RHEL：先查询当前 Python 标准库组件所属的软件包：",
                    f"  dnf provides '*/python{version}/venv/__init__.py' '*/python{version}/ensurepip/__init__.py'",
                    "再用 sudo dnf install 安装查询到的匹配包；已经安装但文件缺失时用 sudo dnf reinstall 修复。",
                ]
            elif "arch" in family:
                instructions += [
                    "Arch Linux：安装或修复完整 Python 标准库：",
                    "  sudo pacman -S python",
                ]
            else:
                instructions.append(
                    "请用本发行版的软件包管理器安装或修复当前版本的 Python 标准库，包含 venv 和 ensurepip。"
                )
        else:
            instructions += [
                "当前为自定义或未识别来源的 Python（例如 pyenv、源码构建）。",
                "请用原安装方式修复或重装完整 Python 3.11+，保留 venv 和 ensurepip 标准库组件；",
                "也可改用其他完整 Python：https://www.python.org/downloads/，并通过 AGENT_PYTHON 指定其绝对路径。",
            ]
    instructions += [
        "修复后，在安装目录用同一个解释器重新检查（保留原先的 --mode 等选项）：",
        f"  AGENT_PYTHON={retry_python} ./install.sh --check",
        "检查通过后去掉 --check 重新安装；若换用其他解释器，请将 AGENT_PYTHON 改为新解释器的绝对路径。",
    ]
    return "\n".join(instructions)


def python_components_report():
    missing = []
    for name in ("venv", "ensurepip"):
        try:
            module = importlib.import_module(name)
            if name == "venv":
                if not callable(module.EnvBuilder):
                    raise ImportError
            elif not module.version():
                raise ImportError
        except (ImportError, AttributeError):
            missing.append(name)
    if missing:
        return [
            (
                "ERROR",
                "安装组件",
                "缺少或不可用：" + ", ".join(missing) + "\n" + python_component_guidance(),
            )
        ]
    return [("OK", "安装组件", "venv 和 ensurepip 可用")]


def environment_report(root, *, docker=True):
    """Read-only checks; never echo subprocess errors, which may contain credentials."""
    rows = []

    def add(level, label, detail):
        rows.append((level, label, detail))

    add(
        "OK" if sys.version_info >= (3, 11) else "ERROR",
        "Python",
        f"{sys.executable} ({sys.version.split()[0]})；可用 AGENT_PYTHON 指定解释器",
    )
    rows.extend(python_components_report())
    for name in ("pyproject.toml", ".env.example"):
        if not (root / name).is_file():
            add("ERROR", "安装目录", f"缺少 {name}：{root}")
    if not os.access(root, os.W_OK):
        add("ERROR", "安装目录", f"没有写入权限：{root}")
    venv = root / ".venv"
    if venv.is_symlink() or (venv.exists() and not venv.is_dir()):
        add("ERROR", "虚拟环境", ".venv 必须为普通目录；请先手动移走")
    elif venv.exists():
        if not (venv / "pyvenv.cfg").is_file():
            # Recognize our own interrupted preparation, but never an arbitrary directory.
            marker = venv / ".repo-agent-install.json"
            try:
                owned = json.loads(marker.read_text()).get("root") == str(root)
            except (OSError, ValueError, AttributeError):
                owned = False
            if any(venv.iterdir()) and not owned:
                add("ERROR", "虚拟环境", "已有 .venv 不是可识别的虚拟环境，请先手动移走")
        try:
            result = probe(
                [
                    str(environment_python(venv)),
                    "-c",
                    "import json,sys;print(json.dumps([sys.prefix,list(sys.version_info[:2])]))",
                ]
            )
            prefix, version = json.loads(result.stdout) if result.returncode == 0 else (None, None)
            valid = prefix == str(venv) and version == list(sys.version_info[:2])
            for name in ("repo-agent", "repo-agent-build-sandbox"):
                entry = scripts_dir(venv) / name
                # pip can use a shell trampoline for paths containing spaces.
                valid = (
                    valid
                    and entry.is_file()
                    and str(environment_python(venv)) in entry.read_text()[:1024]
                )
            add(
                "OK" if valid else "WARN",
                "虚拟环境",
                (
                    "路径和 Python 版本匹配；重装仍会创建干净环境"
                    if valid
                    else "检测到复制、旧版本或不可用的环境；安装时将保留旧环境并重新创建"
                ),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            add("WARN", "虚拟环境", "现有环境不可用；安装时将保留旧环境并重新创建")
    else:
        add("OK", "虚拟环境", "首次安装，将创建 .venv")
    try:
        count = ssl.create_default_context().cert_store_stats()["x509_ca"]
        add("OK" if count else "WARN", "TLS 证书", f"Python 信任库含 {count} 个 CA；未进行联网验证")
    except (OSError, ssl.SSLError):
        add("ERROR", "TLS 证书", "无法加载信任库；请修复解释器或 SSL_CERT_FILE 配置")
    for key in ("PIP_CERT", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(key)
        if value and not Path(value).expanduser().is_file():
            add("ERROR", "TLS 证书", f"{key} 指向的证书文件不存在")
    if docker:
        try:
            result = probe(["docker", "info", "--format", "{{.ID}}"])
            add(
                "OK" if result.returncode == 0 else "ERROR",
                "Docker",
                (
                    "服务可用"
                    if result.returncode == 0
                    else "服务不可用；启动 Docker 或使用 --skip-sandbox"
                ),
            )
        except (OSError, subprocess.SubprocessError):
            add("ERROR", "Docker", "未安装、未启动或检查超时；可使用 --skip-sandbox")
    return rows


def print_report(rows):
    return print_diagnostics(rows)
