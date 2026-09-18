"""Create project-local launch files while reusing a central Agent installation."""

import argparse
import os
import re
import shlex
from pathlib import Path


def _project_template(agent_home: Path) -> str:
    """Overlay personal defaults on the shipped template, without executing shell code."""
    template = (agent_home / ".env.example").read_text(encoding="utf-8")
    defaults = agent_home / ".env.defaults"
    if not defaults.exists():
        return template
    pattern = re.compile(r"^(?:export )?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    known = {
        match.group(1)
        for line in template.splitlines()
        if (match := pattern.fullmatch(line.strip()))
    }
    values = {}
    for number, line in enumerate(defaults.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise ValueError(f".env.defaults 第 {number} 行格式错误，应为 KEY=VALUE")
        key, value = match.groups()
        if key not in known or key in values:
            raise ValueError(f".env.defaults 第 {number} 行含未知或重复的配置项")
        value = value.strip()
        if value.startswith(('"', "'")) and (len(value) < 2 or value[-1] != value[0]):
            raise ValueError(f".env.defaults 第 {number} 行引号不匹配")
        values[key] = value
    lines = []
    for line in template.splitlines():
        match = pattern.fullmatch(line.strip())
        if match and match.group(1) in values:
            key = match.group(1)
            line = f"{key}={values[key]}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _add_missing_settings(path: Path, template: str) -> bool:
    """Append missing runtime options, retaining all existing values and credentials."""
    if path.is_symlink() or not path.is_file():
        return False
    previous = path.read_text(encoding="utf-8")
    present = set(re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", previous, re.M))
    pending_comments = []
    additions = []
    for line in template.splitlines():
        if not line or line.startswith("#"):
            pending_comments.append(line)
            continue
        key = line.split("=", 1)[0]
        # Existing values, including credentials and explicit empty values, take precedence.
        if key not in present:
            additions.extend(pending_comments)
            additions.append(line)
        pending_comments = []
    if not additions:
        return False
    with path.open("a", encoding="utf-8") as output:
        output.write("\n\n# 新增的 Agent 配置项（保留原有设置）\n")
        output.write("\n".join(additions) + "\n")
    return True


def initialize_project(project: Path, agent_home: Path) -> list[str]:
    project = project.resolve(strict=True)
    agent_home = agent_home.resolve(strict=True)
    if not project.is_dir():
        raise ValueError("任务目录必须是已存在的目录")
    template = _project_template(agent_home)
    ignore = project / ".gitignore"
    if ignore.is_symlink():
        raise ValueError("请先将项目 .gitignore 改为普通文件")
    previous = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    launcher = (
        "#!/usr/bin/env bash\n"
        "set +x\nset -euo pipefail\n"
        "# Override AGENT_HOME in your shell if the shared installation moves.\n"
        f"agent_default_home={shlex.quote(str(agent_home))}\n"
        'agent_home="${AGENT_HOME:-$agent_default_home}"\n'
        'if [[ ! -f "$agent_home/run_agent.sh" ]]; then\n'
        "    printf '找不到 Agent 安装目录，请设置 AGENT_HOME。\\n' >&2\n"
        "    exit 1\nfi\n"
        'if [[ "$agent_home/run_agent.sh" -ef "${BASH_SOURCE[0]}" ]]; then\n'
        "    printf 'AGENT_HOME 应指向共享安装目录，不能指向任务目录。\\n' >&2\n"
        "    exit 1\nfi\n"
        'exec bash "$agent_home/run_agent.sh" "$@"\n'
    )
    messages = []
    for name, content, mode in (
        ("run_agent.sh", launcher, 0o755),
        (".env", template, 0o600),
    ):
        path = project / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            messages.append(f"保留已有文件：{path}")
            if name == ".env" and _add_missing_settings(path, template):
                messages.append(f"已补充缺少的配置项：{path}")
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        messages.append(f"已创建：{path}")
    rules = [".env", ".env.*", "!.env.example", "/logs/"]
    missing = [rule for rule in rules if rule not in previous.splitlines()]
    if missing:
        with ignore.open("a", encoding="utf-8") as output:
            if previous and not previous.endswith("\n"):
                output.write("\n")
            output.write("\n# Coding Agent local configuration and conversations\n")
            output.write("\n".join(missing) + "\n")
    return messages


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化任务目录的 Agent 启动文件")
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--agent-home", type=Path, required=True)
    args = parser.parse_args()
    try:
        for message in initialize_project(Path(args.directory), args.agent_home):
            print(message)
    except (OSError, ValueError) as error:
        parser.exit(1, f"初始化失败：{error}\n")
    print("请填写任务目录中的 .env，然后在该目录执行 ./run_agent.sh。")


if __name__ == "__main__":
    main()
