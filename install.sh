#!/usr/bin/env bash
# Install from a clone without changing the caller's shell or system Python.
set +x
set -euo pipefail
agent_install_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    cat <<'HELP'
用法：./install.sh [--mode native|docker|local] [--languages all|python,typescript,go,cpp] [--with-toolchains | --skip-toolchains] [--no-path] [--bin-dir DIRECTORY] [--check | --recover]
创建 .venv、安装所选模式依赖、生成默认配置并安装命令。首次默认 native，重装沿用原模式。
--check 只检查环境；--recover 只恢复中断的安装。安装失败自动恢复原环境和命令。
需要 Python 3.11+。native 支持 macOS 或 Linux（bubblewrap/libseccomp），默认安装 Python 语言服务；询问是否补齐其他语言，回车跳过。
docker 模式需要已启动的 Docker 并构建镜像；local 仅安装文件/Git 模式。
--with-toolchains 自动补齐；--skip-toolchains 跳过额外补齐。无输入时也跳过。
--languages 限定补齐范围，Python 始终安装；后续用 repo-agent toolchains list/install 查看和补齐。
--skip-sandbox 仅在 docker 模式跳过镜像构建。
系统共享工具链不会随卸载或失败恢复而删除。
命令指向其他安装目录时，先确认再安装；回车默认取消，成功后切换命令，不自动回退。
安装时无需模型或 API Key；安装后编辑提示的配置文件即可。已有配置保持不变。
安装改动会记录，卸载可执行 ./uninstall.sh；预览使用 --dry-run。
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
exec "$agent_python" "$agent_install_dir/cli/setup.py" --bootstrap --agent-home "$agent_install_dir" "$@"
