#!/usr/bin/env bash
set +x
set -euo pipefail
agent_install_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
    printf '卸载需要可用的 Python 3.11+；可用 AGENT_PYTHON 指定，不需要项目 .venv。\n' >&2
    exit 1
fi
exec "$agent_python" "$agent_install_dir/cli/uninstall.py" --agent-home "$agent_install_dir" "$@"
