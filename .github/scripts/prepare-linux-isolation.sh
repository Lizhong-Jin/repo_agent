#!/usr/bin/env bash
# CI-only setup for disposable Ubuntu runners, including the 22.04 release baseline.
set -euo pipefail

sudo apt-get update
sudo apt-get install -y bubblewrap libseccomp2

supports_nested_userns_blocking() {
  local usage
  usage="$(bwrap --help)"
  [[ "$usage" == *--disable-userns* && "$usage" == *--assert-userns-disabled* ]]
}

if ! supports_nested_userns_blocking; then
  # Ubuntu 22.04 ships 0.6.1. Keep its libc baseline, but build a helper that
  # supports both security flags; never remove flags to accommodate an old bwrap.
  version=0.11.0
  digest=988fd6b232dafa04b8b8198723efeaccdb3c6aa9c1c7936219d5791a8b7a8646
  build_dir="$(mktemp -d "${RUNNER_TEMP:?}/bubblewrap.XXXXXX")"
  trap 'rm -rf "$build_dir"' EXIT
  sudo apt-get install -y build-essential libcap-dev meson ninja-build pkg-config curl xz-utils
  curl --fail --location --retry 3 \
    "https://github.com/containers/bubblewrap/releases/download/v${version}/bubblewrap-${version}.tar.xz" \
    --output "$build_dir/source.tar.xz"
  printf '%s  %s\n' "$digest" "$build_dir/source.tar.xz" | sha256sum --check --strict
  tar -xJf "$build_dir/source.tar.xz" -C "$build_dir"
  meson setup "$build_dir/build" "$build_dir/bubblewrap-$version" \
    --buildtype=release -Dtests=false -Dman=disabled -Dselinux=disabled
  ninja -C "$build_dir/build" bwrap
  sudo install -m 0755 "$build_dir/build/bwrap" /usr/local/bin/bwrap
  hash -r
fi

bwrap --version
supports_nested_userns_blocking
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
fi
