"""Mode-specific dependency preparation and probes, usable before pip (stdlib only)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

LANGUAGES = ("python", "typescript", "go", "cpp")
TYPESCRIPT = "5.9.3"
TYPESCRIPT_SERVER = "4.3.4"
GOPLS = "v0.20.0"
HINTS = {
    "python": "运行 repo-agent toolchains install python",
    "typescript": "运行 repo-agent toolchains install typescript",
    "go": "运行 repo-agent toolchains install go",
    "cpp": "运行 repo-agent toolchains install cpp",
}


def tool_path(python):
    prefix = Path(python).absolute().parent.parent
    if sys.platform == "linux":
        return os.pathsep.join(map(str, [
            prefix / "bin", prefix / "lsp/node_modules/.bin", "/usr/local/go/bin",
            "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ]))
    return os.pathsep.join(
        map(
            str,
            [
                prefix / "bin",
                prefix / "lsp/node_modules/.bin",
                "/opt/homebrew/opt/llvm/bin",
                "/usr/local/opt/llvm/bin",
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ],
        )
    )


def selected_languages(value):
    if value == "all":
        return list(LANGUAGES)
    result = list(dict.fromkeys(value.split(",")))
    if not result or any(name not in LANGUAGES for name in result):
        raise ValueError("--languages 应为 all 或 python,typescript,go,cpp 的组合")
    # Python is always included in native installations.
    return list(dict.fromkeys(["python", *result]))


def available_mode(root):
    """Installation preference; never read a task project's configuration or run commands."""
    try:
        record = json.loads((Path(root) / ".repo-agent-install.json").read_text())
        mode = record.get("mode")
        if record.get("root") == str(Path(root).resolve()) and mode in {
            "native",
            "local",
            "docker",
        }:
            return mode
    except (OSError, ValueError, AttributeError):
        pass
    return "native"


def toolchain_report(python, languages="all"):
    rows, available = [], []
    path = tool_path(python)
    for language in selected_languages(languages):
        requirements = {
            "python": (),
            "typescript": ("node", "npm"),
            "go": ("go",),
            "cpp": ("clangd",),
        }[language]
        missing = []
        for name in requirements:
            executable = shutil.which(name, path=path)
            if executable is None:
                missing.append(name)
                continue
            try:
                command = [executable, "version" if name == "go" else "--version"]
                result = subprocess.run(
                    command,
                    env={**os.environ, "PATH": path, "GOTOOLCHAIN": "local", "GOTELEMETRY": "off"},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode:
                    missing.append(name)
                elif name == "go":
                    import re

                    match = re.search(r"go(\d+)\.(\d+)", result.stdout)
                    if not match or tuple(map(int, match.groups())) < (1, 25):
                        missing.append("go")
            except (OSError, subprocess.SubprocessError):
                missing.append(name)
        if missing:
            rows.append(
                (
                    "ERROR",
                    language,
                    "缺少 " + ", ".join(missing) + "；" + HINTS[language],
                )
            )
        else:
            available.append(language)
            rows.append(("OK", language, "安装所需工具链可用"))
    return rows, available


def brew_executable():
    return shutil.which("brew", path=tool_path(sys.executable))


def preparation_report(python, languages="all"):
    rows, available = toolchain_report(python, languages)
    if sys.platform == "linux":
        if any(name not in available for name in selected_languages(languages)):
            rows.append(("ERROR", "系统工具链", "请用发行版包管理器安装 Node.js/npm、Go 1.25+ 或 clangd；不会自动调用 sudo"))
        return rows
    brew = brew_executable()
    missing = [name for name in selected_languages(languages) if name not in available]
    if missing and brew:
        rows = [
            (
                "WARN" if name in missing else level,
                name,
                detail + "；安装时将通过 Homebrew 补齐" if name in missing else detail,
            )
            for level, name, detail in rows
        ]
    elif missing:
        rows.append(
            (
                "ERROR",
                "Homebrew",
                "缺少工具链且未找到 Homebrew；请先安装 Homebrew：https://brew.sh",
            )
        )
    return rows


def prepare_toolchains(python, languages="all"):
    _, available = toolchain_report(python, languages)
    missing = [name for name in selected_languages(languages) if name not in available]
    if missing and sys.platform == "linux":
        raise ValueError("Linux 缺少工具链：" + ", ".join(missing)
                         + "；请先用发行版包管理器安装 Node.js/npm、Go 1.25+ 或 clangd")
    if missing:
        brew = brew_executable()
        if not brew:
            raise ValueError("缺少工具链且未找到 Homebrew；请先安装 Homebrew：https://brew.sh")
        formulas = {"typescript": "node", "go": "go", "cpp": "llvm"}
        packages = [formulas[name] for name in missing]
        print("通过 Homebrew 补齐共享工具链：" + ", ".join(packages), flush=True)
        subprocess.run([brew, "install", *packages], check=True)
    rows, available = toolchain_report(python, languages)
    remaining = [name for name in selected_languages(languages) if name not in available]
    if remaining and missing:
        print(
            "工具链仍不可用，尝试更新对应 Homebrew 包：" + ", ".join(remaining),
            flush=True,
        )
        subprocess.run([brew, "upgrade", *[formulas[name] for name in remaining]], check=True)
        _, available = toolchain_report(python, languages)
    if any(name not in available for name in selected_languages(languages)):
        raise ValueError("Homebrew 执行后仍缺少所选工具链；请检查安装输出与 PATH")
    return available


def native_preflight():
    if sys.platform == "linux":
        import ctypes

        if not shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin"):
            return [("ERROR", "原生沙箱", "缺少 bubblewrap；Debian/Ubuntu 安装 bubblewrap libseccomp2，Fedora 安装 bubblewrap libseccomp")]
        try:
            ctypes.CDLL("libseccomp.so.2")
        except OSError:
            return [("ERROR", "原生沙箱", "缺少 libseccomp.so.2；请安装 libseccomp2 或 libseccomp")]
        return [("OK", "原生沙箱", "bubblewrap/libseccomp 存在；安装后将验证 user namespace、实际隔离和语言服务")]
    if sys.platform != "darwin":
        return [
            (
                "ERROR",
                "原生沙箱",
                "native 仅支持 macOS/Linux；Windows 请通过 WSL2 运行",
            )
        ]
    if not Path("/usr/bin/sandbox-exec").is_file():
        return [
            (
                "ERROR",
                "原生沙箱",
                "缺少 /usr/bin/sandbox-exec；请选择 --mode docker 或 --mode local",
            )
        ]
    return [("OK", "原生沙箱", "sandbox-exec 存在；安装后将验证实际隔离和语言服务")]


def install_language_servers(root, languages):
    """Managed servers live inside .venv: rollback/uninstall already own this directory."""
    python = str(root / ".venv/bin/python")
    env = {**os.environ, "PATH": tool_path(python)}
    if "typescript" in languages:
        subprocess.run(
            [
                shutil.which("npm", path=env["PATH"]),
                "install",
                "--prefix",
                str(root / ".venv/lsp"),
                "--no-save",
                "--package-lock=false",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "typescript@" + TYPESCRIPT,
                "typescript-language-server@" + TYPESCRIPT_SERVER,
            ],
            env=env,
            cwd=root,
            check=True,
        )
    if "go" in languages:
        env.update(GOBIN=str(root / ".venv/bin"), GOTOOLCHAIN="local", GOWORK="off")
        subprocess.run(
            [
                shutil.which("go", path=env["PATH"]),
                "install",
                "golang.org/x/tools/gopls@" + GOPLS,
            ],
            env=env,
            cwd=root,
            check=True,
        )


def service_report(root, *, mode, languages=None, image="repo-agent-sandbox:v1"):
    """Actual symbol tests, with fake source files and no user project mounts or credentials."""
    if mode == "local":
        return [("OK", "执行模式", "local 仅提供文件/Git 工具，无需语言服务")]
    selected = list(LANGUAGES) if languages is None else languages
    arguments = ["--json", "--languages", ",".join(selected)]
    container = None
    if mode == "native":
        command = [
            str(root / ".venv/bin/python"),
            "-I",
            "-m",
            "sandbox.lsp_smoke",
            "--mode",
            "native",
            *arguments,
        ]
        env = {**os.environ, "PATH": tool_path(root / ".venv/bin/python")}
    else:
        container = "repo-agent-lsp-check-" + uuid4().hex
        command = [
            "docker",
            "run",
            "--name",
            container,
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            "65534:65534",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--pids-limit",
            "128",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=256m",
            "--entrypoint",
            "python",
            image,
            "-I",
            "-m",
            "sandbox.lsp_smoke",
            *arguments,
        ]
        env = os.environ.copy()
    try:
        result = subprocess.run(
            command,
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=45 + 90 * len(selected),
        )
        rows = json.loads(result.stdout)
        if (
            not isinstance(rows, list)
            or not rows
            or any(
                not isinstance(row, list)
                or len(row) != 3
                or row[0] not in {"OK", "ERROR"}
                or not all(isinstance(value, str) for value in row)
                for row in rows
            )
        ):
            raise ValueError
        if result.returncode and not any(row[0] == "ERROR" for row in rows):
            raise ValueError
        return rows
    except (OSError, ValueError, subprocess.SubprocessError):
        return [
            (
                "ERROR",
                "语言服务实测",
                "无法完成诊断（依赖缺失、沙箱不可用或超时）；请重新安装并检查运行模式",
            )
        ]
    finally:
        if container:
            try:
                subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                print(f"诊断容器清理未确认，请检查：{container}", file=sys.stderr)


LANGUAGE_LABELS = {
    "python": ("Python", "Python 3.11+", "pylsp"),
    "typescript": (
        "JavaScript / TypeScript / JSX / TSX",
        "Node.js + npm",
        "typescript-language-server + TypeScript",
    ),
    "go": ("Go", "Go 1.25+", "gopls"),
    "cpp": ("C / C++ / CUDA 文件", "LLVM / clangd", "clangd"),
}


def command_works(command, env):
    if not command[0]:
        return False
    try:
        return (
            subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def language_status(root):
    """Offline presence/startup checks in exactly the native runtime's search path."""
    python = root / ".venv/bin/python"
    env = {
        **os.environ,
        "PATH": tool_path(python),
        "GOTOOLCHAIN": "local",
        "GOTELEMETRY": "off",
    }
    _, available = toolchain_report(python)
    python_ready = command_works([str(python), "--version"], env)
    commands = {
        "python": [[str(python), "-I", "-m", "pylsp", "--version"]],
        "typescript": [
            [shutil.which(name, path=env["PATH"]), "--version"]
            for name in ("typescript-language-server", "tsc")
        ],
        "go": [[shutil.which("gopls", path=env["PATH"]), "version"]],
        "cpp": [[shutil.which("clangd", path=env["PATH"]), "--version"]],
    }
    return [
        {
            "language": name,
            "toolchain": python_ready if name == "python" else name in available,
            "service": all(command_works(command, env) for command in commands[name]),
        }
        for name in LANGUAGES
    ]


def print_language_status(root):
    rows = language_status(root)
    print("宿主机语言服务（native 模式；已安装表示能启动，实际符号查询用 repo-agent doctor）：")
    print("语言 | 工具链 | 语言服务 | 补齐命令")
    for row in rows:
        name = row["language"]
        label, chain, service = LANGUAGE_LABELS[name]
        chain_state = "已安装" if row["toolchain"] else "缺失或不可用"
        service_state = "已安装" if row["service"] else "缺失或不可用"
        print(
            f"{label} | {chain}: {chain_state} | {service}: {service_state} | repo-agent toolchains install {name}"
        )
    print("查看列表：repo-agent toolchains list；补齐全部：repo-agent toolchains install all")
    print("CUDA 文件复用 clangd；完整 CUDA 编译环境使用 CUDA 镜像。")
    if available_mode(root) != "native":
        print(
            "当前安装并非 native 模式；此表不代表 Docker 镜像状态。镜像用 repo-agent doctor --mode docker 检查。"
        )
    return rows
