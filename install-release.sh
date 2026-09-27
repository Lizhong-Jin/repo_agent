#!/usr/bin/env bash
set +x
set -euo pipefail
agent_root="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$agent_root/scripts/installer-entry.sh" "$agent_root" release "$@"
