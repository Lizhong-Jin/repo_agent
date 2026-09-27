#!/usr/bin/env bash
# Stdlib-free bootstrap: do not require a host Python to install our Python.
set -euo pipefail
umask 077
agent_root="$1"
shift
agent_offline=0
agent_readonly=0
for agent_arg in "$@"; do
    case "$agent_arg" in
        --offline) agent_offline=1 ;;
        --check|--recover) agent_readonly=1 ;;
    esac
done
case "$(uname -s)" in
    Darwin) agent_os=macos ;;
    Linux) agent_os=linux ;;
    *) echo '受管 Python 暂仅支持 macOS/Linux（含 WSL2）' >&2; exit 1 ;;
esac
case "$(uname -m)" in
    arm64|aarch64) agent_arch=arm64 ;;
    x86_64|amd64) agent_arch=x86_64 ;;
    *) echo '不支持的 CPU 架构' >&2; exit 1 ;;
esac
agent_target="$agent_os-$agent_arch"
agent_version='' agent_hash='' agent_url=''
while read -r agent_name agent_ver agent_digest agent_download; do
    if [[ "$agent_name" == "$agent_target" ]]; then
        agent_version="$agent_ver" agent_hash="$agent_digest" agent_url="$agent_download"
        break
    fi
done < "$agent_root/runtime/python.lock"
[[ "$agent_hash" =~ ^[0-9a-f]{64}$ && "$agent_url" == https://* ]] || {
    echo '缺少有效的受管 Python 锁定记录' >&2; exit 1;
}
if [[ -f "$agent_root/runtime/target" && "$(cat "$agent_root/runtime/target")" != "$agent_target" ]]; then
    echo "安装包平台不匹配：当前为 $agent_target" >&2; exit 1
fi
agent_cache="${AGENT_PYTHON_CACHE:-${XDG_DATA_HOME:-$HOME/.local/share}/repo-agent/runtimes}"
agent_id="$agent_version-$agent_target-${agent_hash:0:12}"
agent_destination="$agent_cache/$agent_id"
if [[ -L "$agent_destination" ]]; then
    echo '受管运行时目录不能是符号链接' >&2; exit 1
fi
if [[ -f "$agent_destination/.verified" && "$(cat "$agent_destination/.verified")" == "$agent_hash" && -x "$agent_destination/python/bin/python3" ]]; then
    printf '%s\n' "$agent_destination/python/bin/python3"
    exit 0
fi
if [[ "$agent_readonly" == 1 ]]; then
    echo '尚无已安装的受管 Python；请先正常安装，或用 AGENT_PYTHON 指定解释器进行检查/恢复' >&2
    exit 1
fi
mkdir -p "$agent_cache"
agent_cache="$(cd -P -- "$agent_cache" && pwd)"
agent_destination="$agent_cache/$agent_id"
agent_lock="$agent_destination.lock"
mkdir "$agent_lock" 2>/dev/null || {
    echo "运行时正被安装，或上次安装中断；确认没有安装进程后删除锁目录：$agent_lock" >&2; exit 1;
}
agent_stage=''
trap '[[ -z "$agent_stage" ]] || rm -rf -- "$agent_stage"; rmdir -- "$agent_lock"' EXIT
[[ ! -e "$agent_destination" ]] || { echo '已有不完整运行时，请移走后重试' >&2; exit 1; }
agent_stage="$(mktemp -d "$agent_cache/.python-XXXXXXXX")"
agent_archive="${AGENT_PYTHON_ARCHIVE:-$agent_root/runtime/python.tar.gz}"
if [[ -n "${AGENT_PYTHON_ARCHIVE:-}" && ! -f "$agent_archive" ]]; then
    echo "指定的 Python 压缩包不存在：$agent_archive" >&2; exit 1
fi
if [[ ! -f "$agent_archive" ]]; then
    [[ "$agent_offline" == 0 ]] || { echo '离线安装缺少 Python；设置 AGENT_PYTHON_ARCHIVE 或预先安装受管运行时' >&2; exit 1; }
    agent_archive="$agent_stage/download.tar.gz"
    echo "下载受管 Python $agent_version ($agent_target)…" >&2
    agent_retries="${AGENT_INSTALL_RETRIES:-2}"
    agent_timeout="${AGENT_INSTALL_TIMEOUT:-600}"
    [[ "$agent_retries" =~ ^[0-5]$ && "$agent_timeout" =~ ^[0-9]{1,4}$ ]] && \
        (( 10#$agent_timeout >= 1 && 10#$agent_timeout <= 3600 )) || {
        echo 'AGENT_INSTALL_RETRIES 须为 0～5；AGENT_INSTALL_TIMEOUT 须为 1～3600' >&2; exit 1;
    }
    if [[ -n "${AGENT_INSTALL_PROXY:-}" ]]; then
        export HTTPS_PROXY="$AGENT_INSTALL_PROXY" HTTP_PROXY="$AGENT_INSTALL_PROXY"
        export https_proxy="$AGENT_INSTALL_PROXY" http_proxy="$AGENT_INSTALL_PROXY"
    fi
    curl --fail --location --proto '=https' --proto-redir '=https' \
        --retry "$agent_retries" --connect-timeout 20 --max-time "$agent_timeout" \
        "$agent_url" -o "$agent_archive" >&2
fi
if command -v sha256sum >/dev/null 2>&1; then
    agent_actual="$(sha256sum "$agent_archive")"
else
    agent_actual="$(shasum -a 256 "$agent_archive")"
fi
[[ "${agent_actual%% *}" == "$agent_hash" ]] || { echo 'Python 压缩包 SHA256 校验失败' >&2; exit 1; }
# Only the exact pinned upstream archive reaches tar; includes its license files.
mkdir "$agent_stage/payload"
tar -xzf "$agent_archive" -C "$agent_stage/payload"
"$agent_stage/payload/python/bin/python3" -I -c 'import sys, ssl, ctypes, venv, ensurepip; assert sys.version_info[:3] == tuple(map(int, sys.argv[1].split(".")))' "$agent_version"
printf '%s\n' "$agent_hash" > "$agent_stage/payload/.verified"
mv "$agent_stage/payload" "$agent_destination"
printf '%s\n' "$agent_destination/python/bin/python3"
