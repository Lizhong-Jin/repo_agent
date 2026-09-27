#!/usr/bin/env bash
set +x
set -euo pipefail
agent_install_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
agent_python="${AGENT_PYTHON:-}"
[[ "$agent_python" != system ]] || agent_python=""
if [[ -z "$agent_python" && -x "$agent_install_dir/.venv/bin/python" ]]; then
    agent_python="$agent_install_dir/.venv/bin/python"
fi
if [[ -z "$agent_python" && -f "$agent_install_dir/scripts/bootstrap-python.sh" ]]; then
    agent_python="$(/bin/bash "$agent_install_dir/scripts/bootstrap-python.sh" "$agent_install_dir" --recover 2>/dev/null || true)"
fi
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
    printf '卸载需要已安装的 Agent/受管 Python 或系统 Python 3.11+；也可用 AGENT_PYTHON 指定。\n' >&2
    exit 1
fi
exec "$agent_python" "$agent_install_dir/cli/uninstall.py" --agent-home "$agent_install_dir" "$@"
