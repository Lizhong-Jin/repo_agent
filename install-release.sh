#!/usr/bin/env bash
set -euo pipefail
agent_release_root="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$agent_release_root/install.sh" --release "$@"
