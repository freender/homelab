#!/bin/bash
# Run as the desktop user on solar. Builds ./bluetoothd with build.sh inside a
# throwaway fedora:45 container; install it afterwards with `sudo ./install.sh`.
set -euo pipefail
cd "$(dirname "$0")"
podman run --rm \
  -v "$PWD:/build:Z" \
  -v "$PWD/patches:/patches:ro,Z" \
  registry.fedoraproject.org/fedora:45 \
  bash /build/build.sh
