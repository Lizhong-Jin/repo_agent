#!/usr/bin/env bash
# Start the agent in the caller's directory and record the terminal conversation.
set +x  # Never trace credentials, even when invoked using bash -x.
set -euo pipefail

agent_root="$(pwd -P)"
agent_script="${BASH_SOURCE[0]}"
# Allow a symlink to this launcher to be placed on PATH.
while [[ -L "$agent_script" ]]; do
    agent_link_dir="$(cd -P -- "$(dirname -- "$agent_script")" && pwd)"
    agent_script="$(readlink "$agent_script")"
    [[ "$agent_script" = /* ]] || agent_script="$agent_link_dir/$agent_script"
done
agent_dir="$(cd -P -- "$(dirname -- "$agent_script")" && pwd)"
agent_python="$agent_dir/.venv/bin/python"

if [[ ! -f "$agent_dir/.venv/bin/activate" || ! -x "$agent_python" ]]; then
    printf '找不到 Agent 虚拟环境，请先在 %s 中创建 .venv 并安装项目。\n' "$agent_dir" >&2
    exit 1
fi
if [[ "${1:-}" == --init ]]; then
    shift
    exec "$agent_python" "$agent_dir/cli/init_project.py" --agent-home "$agent_dir" "$@"
fi
if [[ "${1:-}" == --build-sandbox ]]; then
    shift
    cd -- "$agent_dir"
    exec "$agent_python" -m sandbox.build "$@"
fi
# shellcheck source=/dev/null
source "$agent_dir/.venv/bin/activate"

# Parse literal KEY=VALUE entries instead of executing .env as a shell script.
# Supports optional export, surrounding quotes, blank lines and full-line comments.
agent_env_file="${AGENT_ENV_FILE:-$agent_root/.env}"
if [[ -n "${AGENT_ENV_FILE:-}" && ! -f "$agent_env_file" ]]; then
    printf '配置文件不存在：%s\n' "$agent_env_file" >&2
    exit 1
fi
if [[ -f "$agent_env_file" ]]; then
    # File tools also protect a custom configuration filename. Do not turn an
    # absent optional .env into an explicit (and therefore required) override.
    export AGENT_ENV_FILE="$agent_env_file"
    agent_line_number=0
    while IFS= read -r agent_line || [[ -n "$agent_line" ]]; do
        agent_line_number=$((agent_line_number + 1))
        agent_line="${agent_line%$'\r'}"
        agent_line="${agent_line#"${agent_line%%[![:space:]]*}"}"
        agent_line="${agent_line%"${agent_line##*[![:space:]]}"}"
        [[ -z "$agent_line" || "$agent_line" == \#* ]] && continue
        agent_line="${agent_line#export }"
        if [[ ! "$agent_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
            printf '配置文件第 %s 行格式错误，应为 KEY=VALUE。\n' "$agent_line_number" >&2
            exit 1
        fi
        agent_key="${BASH_REMATCH[1]}"
        agent_value="${BASH_REMATCH[2]}"
        case "$agent_key" in
            LLM_PROVIDER|LLM_MODEL|LLM_BASE_URL|LLM_TEMPERATURE|LLM_TOOL_CHOICE|LLM_THINKING|LLM_REASONING_EFFORT|LLM_THINKING_BUDGET|LLM_TIMEOUT|LLM_STREAM|LLM_CONNECT_TIMEOUT|LLM_WRITE_TIMEOUT|LLM_POOL_TIMEOUT|LLM_CONTEXT_WINDOW|LLM_MAX_RETRIES|LLM_RETRY_DELAY|LLM_MAX_RETRY_DELAY|LLM_EXTRA_JSON|AGENT_MAX_STEPS|AGENT_MAX_OUTPUT_TOKENS|AGENT_SYSTEM_PROMPT|AGENT_LOG_DIR|AGENT_SANDBOX_WRITEBACK|AGENT_SANDBOX_VERIFY_COMMAND|DEEPSEEK_API_KEY|DASHSCOPE_API_KEY|MOONSHOT_API_KEY|ZHIPU_API_KEY|ARK_API_KEY|MINIMAX_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY) ;;
            *) continue ;;
        esac
        agent_value="${agent_value#"${agent_value%%[![:space:]]*}"}"
        agent_value="${agent_value%"${agent_value##*[![:space:]]}"}"
        if [[ "$agent_value" == \"* || "$agent_value" == \'* ]]; then
            agent_quote="${agent_value:0:1}"
            if [[ ${#agent_value} -lt 2 || "${agent_value: -1}" != "$agent_quote" ]]; then
                printf '配置文件第 %s 行引号不匹配。\n' "$agent_line_number" >&2
                exit 1
            fi
            agent_value="${agent_value:1:${#agent_value}-2}"
        fi
        # Explicit non-empty environment values take precedence over .env.
        if [[ -z "${!agent_key:-}" ]]; then
            export "$agent_key=$agent_value"
        fi
    done < "$agent_env_file"
fi
unset agent_value agent_line
# Let the CLI apply user configuration before choosing its built-in provider.

agent_platform="$(uname -s)"
case "$agent_platform" in
    Darwin|Linux) ;;
    *) printf '启动脚本仅支持 macOS 和 Linux。\n' >&2; exit 1 ;;
esac

agent_log_dir="${AGENT_LOG_DIR:-$agent_root/logs}"
mkdir -p -- "$agent_log_dir"
agent_log_dir="$(cd -P -- "$agent_log_dir" && pwd)"
agent_log_file="$agent_log_dir/session_$(date '+%Y%m%d_%H%M%S')_$$.log"
# Conversation logs are readable by the owner only; do not overwrite an old log.
(
    umask 077
    set -o noclobber
    : > "$agent_log_file"
)
{
    printf '开始时间：%s\n项目目录：%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$agent_root"
    if [[ $# -gt 0 ]]; then
        printf '启动参数：'
        printf '%q ' "$@"
        printf '\n'
    fi
} >> "$agent_log_file"
# Share the session name; tracing writes its own files without going through the terminal.
export AGENT_LOG_DIR="$agent_log_dir"
agent_session_name="$(basename -- "$agent_log_file" .log)"
export AGENT_SESSION_ID="$agent_session_name"

# Root always uses the invocation directory, not the install directory.
# -a preserves our header; -F flushes promptly; script preserves the child exit code.
if [[ "$agent_platform" == Darwin ]]; then
    exec /usr/bin/script -qFa "$agent_log_file" "$agent_python" -u -m cli.main "$@" --root "$agent_root"
fi
# util-linux script accepts a command string. Quote every argument for bash,
# explicitly selecting that shell so spaces and shell metacharacters stay literal.
printf -v agent_command '%q ' "$agent_python" -u -m cli.main "$@" --root "$agent_root"
SHELL=/bin/bash exec script -qefa -c "$agent_command" "$agent_log_file"
