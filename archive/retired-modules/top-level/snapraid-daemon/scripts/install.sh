#!/bin/bash

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
ENV_FILE="$BUILD_DIR/snapraid-daemon.env"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$ENV_FILE" "$ENV_FILE" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1

# shellcheck source=/dev/null
source "$ENV_FILE"
load_file_map

print_header "SnapRAID Daemon"

configure_traefik_route() {
    [[ -n "$TRAEFIK_FILE_CONFIG_PATH" ]] || return 0

    TRAEFIK_FILE_CONFIG_PATH="$TRAEFIK_FILE_CONFIG_PATH" \
    TRAEFIK_ROUTE_HOST="$TRAEFIK_ROUTE_HOST" \
    TRAEFIK_SERVICE_NAME="$TRAEFIK_SERVICE_NAME" \
    TRAEFIK_SERVICE_URL="$TRAEFIK_SERVICE_URL" \
    python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["TRAEFIK_FILE_CONFIG_PATH"])
route_host = os.environ["TRAEFIK_ROUTE_HOST"]
service_name = os.environ["TRAEFIK_SERVICE_NAME"]
service_url = os.environ["TRAEFIK_SERVICE_URL"]

text = path.read_text(encoding="utf-8")
if f"Host(`{route_host}`)" not in text:
    raise SystemExit(f"missing Traefik router for {route_host} in {path}")

lines = text.splitlines(keepends=True)
in_services = False
in_service = False
changed = False
for index, line in enumerate(lines):
    stripped = line.strip()
    if line.startswith("  services:"):
        in_services = True
        in_service = False
        continue
    if in_services and line.startswith("  middlewares:"):
        break
    if in_services and line.startswith(f"    {service_name}:"):
        in_service = True
        continue
    if in_services and in_service and line.startswith("    ") and not line.startswith("      ") and stripped.endswith(":"):
        raise SystemExit(f"missing service URL for Traefik service {service_name}")
    if in_services and in_service and stripped.startswith("- url:"):
        replacement = f"          - url: {service_url}\n"
        if line != replacement:
            lines[index] = replacement
            changed = True
        break
else:
    raise SystemExit(f"missing Traefik service {service_name} in {path}")

if changed:
    path.write_text("".join(lines), encoding="utf-8")
PY
    print_ok "Traefik route $TRAEFIK_ROUTE_HOST -> $TRAEFIK_SERVICE_URL"
}

if ! command -v snapraid >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q snapraid
    print_ok "snapraid installed"
else
    print_sub "snapraid already installed"
fi

installed_version="$(dpkg-query -W -f='${Version}' snapraid-daemon 2>/dev/null || true)"

if [[ "$installed_version" != "$SNAPRAID_DAEMON_VERSION" ]]; then
    apt-get update -q
    apt-get install -y -q ca-certificates curl

    package_path="/tmp/snapraid-daemon_${SNAPRAID_DAEMON_VERSION}_amd64.deb"
    curl -fsSL "$SNAPRAID_DAEMON_DEB_URL" -o "$package_path"
    printf '%s  %s\n' "$SNAPRAID_DAEMON_SHA256" "$package_path" | sha256sum -c -
    apt-get install -y -q "$package_path"
    print_ok "snapraid-daemon $SNAPRAID_DAEMON_VERSION installed"
else
    print_sub "snapraid-daemon $SNAPRAID_DAEMON_VERSION already installed"
fi

config_changed=false
rc=0
install_build_file snapraidd.conf || rc=$?
if [[ $rc -eq 0 ]]; then
    config_changed=true
elif [[ $rc -ne 1 ]]; then
    exit "$rc"
fi

mkdir -p /var/log/snapraid
systemctl daemon-reload
systemctl enable --now snapraidd.service
if [[ "$config_changed" == "true" ]]; then
    systemctl restart snapraidd.service
fi

configure_traefik_route

print_ok "snapraidd.service enabled"
