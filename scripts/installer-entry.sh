#!/usr/bin/env bash
# Shared Python selection and dispatch for source and release entry points.
set +x
set -euo pipefail
agent_install_dir="$1"
agent_entry="$2"
shift 2
if [[ "$agent_entry" == source && "${1:-}" == --release ]]; then
    agent_entry=release
    shift
fi
agent_help=0
for agent_argument in "$@"; do
    [[ "$agent_argument" != --help && "$agent_argument" != -h ]] || agent_help=1
done
if [[ "$agent_help" == 1 ]]; then
    if [[ "$agent_entry" == release ]]; then
        printf '%s\n' '用法：./install-release.sh [安装选项 | --check | --recover | --uninstall]' \
            '发行安装选项：--archive FILE、--sha256 HASH、--data-dir DIRECTORY。' \
            '卸载：--uninstall [--dry-run] [--purge] [--remove-image]；默认保留配置，不回退旧版本。'
    elif [[ "$agent_entry" == uninstall ]]; then
        printf '%s\n' '用法：./uninstall.sh [--dry-run] [--purge] [--remove-image]'
        exit 0
    else
        printf '%s\n' '用法：./install.sh [安装选项 | --check | --recover]' '卸载：./uninstall.sh [--dry-run] [--purge] [--remove-image]'
    fi
    cat <<'HELP'
安装选项： [--mode native|docker|local] [--languages all|python,typescript,go,cpp] [--with-toolchains | --skip-toolchains] [--no-path] [--bin-dir DIRECTORY] [--check | --recover]
创建 .venv、安装所选模式依赖、生成默认配置并安装命令。首次默认 native，重装沿用原模式。
--check 只检查环境；--recover 只恢复中断的安装。核心安装失败恢复原环境和命令；额外工具链失败保留核心安装。
默认复用或下载锁定的独立 Python；AGENT_PYTHON=system 搜索系统 Python，或指定解释器绝对路径。
--offline --wheelhouse DIRECTORY 使用本地依赖；AGENT_PYTHON_ARCHIVE 可指定本地 Python 运行时压缩包。
native 支持 macOS 或 Linux（bubblewrap/libseccomp），默认安装 Python 语言服务；询问是否补齐其他语言，回车跳过。
docker 模式需要已启动的 Docker 并构建镜像；local 仅安装文件/Git 模式。
--with-toolchains 自动补齐；--skip-toolchains 跳过额外补齐。无输入时也跳过。
--languages 限定补齐范围，Python 始终安装；后续用 repo-agent toolchains list/install 查看和补齐。
--skip-sandbox 仅在 docker 模式跳过镜像构建。
系统共享工具链不会随卸载或失败恢复而删除。
下载临时故障默认重试 2 次；AGENT_INSTALL_RETRIES / AGENT_INSTALL_TIMEOUT 可设置次数和单步超时（秒）。
代理/包源可通过 AGENT_INSTALL_PROXY、AGENT_INSTALL_PYPI_INDEX、AGENT_INSTALL_NPM_REGISTRY、AGENT_INSTALL_GOPROXY 指定。
命令指向其他安装目录时，先确认再安装；回车默认取消，成功后切换命令，不自动回退。
安装时无需模型或 API Key；安装后编辑提示的配置文件即可。已有配置保持不变。
HELP
    exit 0
fi
agent_python=""
agent_partial_python=""
agent_recover=0
agent_readonly=0
[[ "$agent_entry" != uninstall ]] || agent_recover=1
for agent_argument in "$@"; do
    case "$agent_argument" in
        --recover|--uninstall) agent_recover=1 ;;
        --check) agent_readonly=1 ;;
    esac
done
[[ "$agent_recover" == 0 ]] || agent_readonly=1
agent_probe='import sys
if sys.version_info < (3, 11):
    print("版本低于 Python 3.11")
    sys.exit(1)
if sys.argv[1] == "1":
    sys.exit(0)
missing = []
for name in ("venv", "ensurepip"):
    try:
        module = __import__(name)
        if name == "venv":
            assert callable(module.EnvBuilder)
        else:
            assert module.version()
    except (ImportError, AttributeError, AssertionError):
        missing.append(name)
if missing:
    print("缺少或不可用：" + ", ".join(missing))
    sys.exit(2)
'
agent_try_python() {
    local agent_candidate="$1" agent_reason agent_code
    if agent_reason=$("$agent_candidate" -I -B -c "$agent_probe" "$agent_recover" 2>/dev/null); then
        agent_python="$agent_candidate"
        return 0
    else
        agent_code=$?
        if [[ "$agent_code" == 2 && -z "$agent_partial_python" ]]; then
            agent_partial_python="$agent_candidate"
        fi
        printf '跳过 Python %s：%s\n' "$agent_candidate" "${agent_reason:-无法运行}" >&2
        return 1
    fi
}
# Maintenance never downloads Python and can use a stdlib-only system interpreter.
if [[ -z "${AGENT_PYTHON:-}" ]]; then
    if [[ "$agent_readonly" == 1 ]]; then
        for agent_existing in "$agent_install_dir/.venv/bin/python" "$agent_install_dir/.repo-agent-install-transaction/venv/bin/python"; do
            if [[ -x "$agent_existing" ]] && agent_try_python "$agent_existing"; then
                break
            fi
        done
        if [[ -z "$agent_python" ]]; then
            agent_cached="$(/bin/bash "$agent_install_dir/scripts/bootstrap-python.sh" "$agent_install_dir" --recover 2>/dev/null || true)"
            [[ -z "$agent_cached" ]] || agent_try_python "$agent_cached" || true
        fi
    else
        AGENT_PYTHON="$(/bin/bash "$agent_install_dir/scripts/bootstrap-python.sh" "$agent_install_dir" "$@")"
        export AGENT_PYTHON
    fi
elif [[ "$AGENT_PYTHON" == system ]]; then
    unset AGENT_PYTHON
fi
if [[ -n "$agent_python" ]]; then
    :
elif [[ -n "${AGENT_PYTHON:-}" ]]; then
    # An explicit selection must never silently switch to a different interpreter.
    agent_candidate=$(command -v -- "$AGENT_PYTHON" || true)
    if [[ -n "$agent_candidate" ]]; then
        agent_try_python "$agent_candidate" || true
    else
        printf 'AGENT_PYTHON 指定的解释器不存在：%s\n' "$AGENT_PYTHON" >&2
    fi
else
    # Search every PATH directory, not just the first python3 returned by command -v.
    IFS=: read -r -a agent_search_dirs <<< "${PATH:-}"
    agent_search_dirs+=(/opt/homebrew/bin /usr/local/bin /usr/bin)
    agent_seen=""
    for agent_dir in "${agent_search_dirs[@]}"; do
        [[ -n "$agent_dir" && -d "$agent_dir" ]] || continue
        for agent_candidate in "$agent_dir/python3" "$agent_dir"/python3.[0-9]*; do
            [[ -f "$agent_candidate" && -x "$agent_candidate" ]] || continue
            [[ "${agent_candidate##*/}" =~ ^python3(\.[0-9]+)?$ ]] || continue
            case "$agent_seen" in *"|$agent_candidate|"*) continue ;; esac
            agent_seen="$agent_seen|$agent_candidate|"
            if agent_try_python "$agent_candidate"; then break 2; fi
        done
    done
fi
if [[ -z "$agent_python" ]]; then
    printf '未找到组件齐全的 Python 3.11+；可用 AGENT_PYTHON 指定完整解释器。\n' >&2
    if [[ -n "$agent_partial_python" ]]; then
        "$agent_partial_python" -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from maintenance import python_components_report, print_report; print_report(python_components_report())' "$agent_install_dir/cli" || true
    else
        printf 'macOS 可安装完整 Python：https://www.python.org/downloads/macos/；Linux 请通过发行版包管理器安装 Python 3.11+ 及匹配的 venv 组件。\n' >&2
    fi
    exit 1
fi
printf '使用 Python：%s\n' "$agent_python"
if [[ "$agent_entry" == release ]]; then
    exec "$agent_python" -B "$agent_install_dir/cli/release_install.py" "$@"
fi
if [[ "$agent_entry" == uninstall ]]; then
    exec "$agent_python" -B "$agent_install_dir/cli/uninstall.py" --agent-home "$agent_install_dir" "$@"
fi
exec "$agent_python" "$agent_install_dir/cli/setup.py" --bootstrap --agent-home "$agent_install_dir" "$@"
