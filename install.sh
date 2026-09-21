#!/usr/bin/env bash
# Install from a clone without changing the caller's shell or system Python.
set +x
set -euo pipefail
agent_install_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    cat <<'HELP'
用法：./install.sh [--skip-sandbox] [--no-path] [--bin-dir DIRECTORY]
创建 .venv、安装依赖、生成默认配置文件、构建 Docker 镜像并安装 repo-agent 命令。
需要 Python 3.11+ 和已启动的 Docker。--skip-sandbox 仅跳过镜像构建。
安装时无需模型或 API Key；安装后编辑提示的配置文件即可。已有配置保持不变。
HELP
    exit 0
fi
agent_python="${AGENT_PYTHON:-}"
if [[ -z "$agent_python" ]]; then
    for agent_candidate in python3 python3.14 python3.13 python3.12 python3.11; do
        if command -v "$agent_candidate" >/dev/null 2>&1 && \
            "$agent_candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
            agent_python="$agent_candidate"
            break
        fi
    done
fi
if [[ -z "$agent_python" ]] || ! "$agent_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    printf '需要 Python 3.11+；也可以用 AGENT_PYTHON 指定解释器路径。\n' >&2
    exit 1
fi
"$agent_python" -m venv "$agent_install_dir/.venv"
"$agent_install_dir/.venv/bin/python" -m pip install -e "$agent_install_dir"
exec "$agent_install_dir/.venv/bin/python" -m cli.setup --agent-home "$agent_install_dir" "$@"
