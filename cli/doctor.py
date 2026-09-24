"""Read-only diagnostics, also runnable with a base Python when the venv is broken."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .dependencies import available_mode, native_preflight, service_report
    from .install_transaction import TRANSACTION
    from .installation import (
        COMMANDS,
        DEFAULT_IMAGE,
        load_record,
        read_record,
        registry_dir,
        user_config_path,
    )
    from .maintenance import environment_report, print_report, probe
    from .paths import installation_root
else:
    from install_transaction import TRANSACTION
    from installation import (
        COMMANDS,
        DEFAULT_IMAGE,
        load_record,
        read_record,
        registry_dir,
        user_config_path,
    )
    from maintenance import environment_report, print_report, probe
    from paths import installation_root

    from dependencies import available_mode, native_preflight, service_report


def diagnose(root, workspace, *, mode=None, docker=None):
    # Keep the Python API's old docker=False behavior for callers requesting core-only checks.
    mode = mode or (
        "docker" if docker is True else "local" if docker is False else available_mode(root)
    )
    rows = environment_report(root, docker=mode == "docker")
    rows.append(("OK", "执行模式", mode))
    if mode == "native":
        rows += native_preflight()

    def add(level, label, detail):
        rows.append((level, label, detail))

    add("OK", "安装位置", str(root))
    add("OK", "工作目录", str(workspace))
    if (root / TRANSACTION).exists():
        add(
            "ERROR",
            "安装恢复",
            "存在未完成或待清理的安装事务；请在安装目录运行 ./install.sh --recover",
        )
    record = None
    try:
        record = load_record(root)
        if record and record.get("pending_languages"):
            pending = [
                name for name in record["pending_languages"] if name in {"typescript", "go", "cpp"}
            ]
            if pending:
                add(
                    "WARN",
                    "额外语言服务",
                    "尚未完成；补齐：repo-agent toolchains install " + " ".join(pending),
                )
        add(
            "OK" if record and record["status"] == "installed" else "WARN",
            "安装记录",
            record["status"] if record else "未登记；可重新运行 install.sh",
        )
    except (OSError, ValueError):
        add("ERROR", "安装记录", "记录损坏或属于其他目录，请检查复制/移动后的安装")
    for name in COMMANDS:
        command = shutil.which(name)
        expected = root / ".venv/bin" / name
        if command:
            actual = Path(command).resolve()
            add("OK" if actual == expected else "WARN", name, f"{command} → {actual}")
        else:
            add("WARN", name, "PATH 中找不到命令；检查 ~/.local/bin 或安装时指定的 --bin-dir")
        candidates = {
            str(Path(part or ".").absolute() / name)
            for part in os.get_exec_path()
            if (Path(part or ".") / name).is_file()
        }
        if len(candidates) > 1:
            add("WARN", "PATH 重复命令", ", ".join(sorted(candidates)))
    try:
        result = probe(
            [str(root / ".venv/bin/python"), "-I", "-c", "import cli.main, sandbox.build"], cwd=root
        )
        add(
            "OK" if result.returncode == 0 else "ERROR",
            "运行依赖",
            "核心模块可导入" if result.returncode == 0 else "依赖缺失或环境损坏，请重新安装",
        )
    except (OSError, subprocess.SubprocessError):
        add("ERROR", "运行依赖", "虚拟环境不可用，请重新安装")
    add("OK", "用户配置", str(user_config_path()))
    try:
        if not __package__:
            sys.path.insert(0, str(root))
        from cli.config import load_configuration
        from cli.config_command import validate_configuration

        _, sources, project = load_configuration(workspace)
        add("OK", "项目配置", str(project))
        for source in ("用户配置", "项目配置", "环境变量"):
            add("OK", "配置来源", f"{source}提供 {list(sources.values()).count(source)} 个生效项")
        warnings = validate_configuration(workspace)
        add("OK", "配置", "格式及参数组合校验通过；config show 可查看值及来源")
        for warning in warnings:
            add("WARN", "配置", warning)
    except ImportError:
        add("WARN", "配置", "当前解释器缺少依赖，无法完整校验；安装完成后运行 config validate")
    except (OSError, ValueError) as error:
        add("ERROR", "配置", str(error))
    if mode == "docker" and any(level == "OK" and label == "Docker" for level, label, _ in rows):
        try:
            result = probe(["docker", "image", "inspect", "--format", "{{.Id}}", DEFAULT_IMAGE])
            add(
                "OK" if result.returncode == 0 else "WARN",
                "沙箱镜像",
                (
                    DEFAULT_IMAGE
                    if result.returncode == 0
                    else "缺少镜像，可运行 repo-agent-build-sandbox"
                ),
            )
        except (OSError, subprocess.SubprocessError):
            add("WARN", "沙箱镜像", "检查失败或超时")
    try:
        result = probe([str(root / ".venv/bin/python"), "-m", "pip", "check"], cwd=root)
        add(
            "OK" if result.returncode == 0 else "ERROR",
            "Python 依赖一致性",
            "pip check 通过" if result.returncode == 0 else "pip check 未通过，请重新安装",
        )
    except (OSError, subprocess.SubprocessError):
        add("ERROR", "Python 依赖一致性", "无法执行 pip check")
    if mode == "native" and not any(
        level == "ERROR" and label == "原生沙箱" for level, label, _ in rows
    ):
        languages = record.get("languages") if record and record.get("mode") == "native" else None
        if (
            not isinstance(languages, list)
            or not languages
            or any(name not in {"python", "typescript", "go", "cpp"} for name in languages)
        ):
            languages = None
        rows += service_report(root, mode="native", languages=languages)
    elif mode == "docker" and any(
        level == "OK" and label == "沙箱镜像" for level, label, _ in rows
    ):
        rows += service_report(root, mode="docker")
    for path in registry_dir().glob("*.json"):
        try:
            entry = read_record(path)
            if entry["status"] != "uninstalled" and not Path(entry["root"]).is_dir():
                add("WARN", "失效安装记录", f"目录已不存在：{entry['root']}；可能影响共享资源清理")
        except (OSError, ValueError):
            add("WARN", "安装登记表", f"无法读取记录：{path.name}")
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="repo-agent doctor",
        description="诊断依赖并以临时示例实测语言服务，不修改项目或配置，不请求模型、不拉取镜像",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="检查此工作目录的配置")
    parser.add_argument("--mode", choices=["native", "docker", "local"], help="默认沿用安装模式")
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="兼容选项：仅检查 local 模式，不检查执行沙箱和语言服务",
    )
    args = parser.parse_args(argv)
    root = installation_root()
    try:
        ok = print_report(
            diagnose(
                root,
                args.root.expanduser().resolve(),
                mode="local" if args.skip_docker else args.mode,
            )
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"诊断未完成：{error}\n")
    if not ok:
        parser.exit(1)


if __name__ == "__main__":
    main()
