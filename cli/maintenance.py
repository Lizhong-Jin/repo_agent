"""Standard-library maintenance checks and cooperative process locks (macOS/Linux)."""

import fcntl
import json
import os
import ssl
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f"另一个安装、卸载或配置操作正在进行，请稍后重试：{path}") from None
        yield
    finally:
        os.close(fd)  # Keep the inode: unlinking a lock file can split concurrent locks.


def probe(command, *, timeout=15, cwd=None):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=cwd)


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
    try:
        import ensurepip
        import venv

        if not ensurepip.version() or not venv.EnvBuilder:
            raise ImportError
        add("OK", "安装组件", "venv 和 ensurepip 可用")
    except ImportError:
        add("ERROR", "安装组件", "缺少 venv 或 ensurepip；请安装完整 Python 环境")
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
                    str(venv / "bin/python"),
                    "-c",
                    "import json,sys;print(json.dumps([sys.prefix,list(sys.version_info[:2])]))",
                ]
            )
            prefix, version = json.loads(result.stdout) if result.returncode == 0 else (None, None)
            valid = prefix == str(venv) and version == list(sys.version_info[:2])
            for name in ("repo-agent", "repo-agent-build-sandbox"):
                entry = venv / "bin" / name
                # pip can use a shell trampoline for paths containing spaces.
                valid = (
                    valid
                    and entry.is_file()
                    and str(venv / "bin/python") in entry.read_text()[:1024]
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
    for level, label, detail in rows:
        print(f"[{level}] {label}：{detail}")
    return not any(level == "ERROR" for level, _, _ in rows)
