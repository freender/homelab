#!/bin/bash
# install.sh - Install SSH config on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}
TRAEFIK_SYNC_ROOT="$HOME/traefik-sync"
TRAEFIK_SYNC_DIR="$TRAEFIK_SYNC_ROOT/.ssh"

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    source "$SCRIPT_DIR/lib/utils.sh"
else
    backup_config() {
        local path="$1"
        [[ -e "$path" ]] || return 0
        cp -r "$path" "${path}.bak.$(date +%Y%m%d%H%M%S)"
    }
    print_sub() { echo "    $*"; }
    print_warn() { echo "    ✗ Warning: $*"; }
fi

copy_if_changed() {
    local src="$1"
    local dst="$2"
    local label="$3"

    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f "$dst" ]] || ! cmp -s "$src" "$dst"; then
        cp "$src" "$dst"
        print_sub "Updated $label"
        return 0
    fi

    print_sub "$label unchanged; skipping update"
    return 1
}

ensure_traefik_sync_key() {
    if [[ ! -f "$TRAEFIK_SYNC_DIR/id_ed25519" ]]; then
        ssh-keygen -t ed25519 -f "$TRAEFIK_SYNC_DIR/id_ed25519" -N "" -C "${HOST}-traefik-sync" >/dev/null
        print_sub "Generated $TRAEFIK_SYNC_DIR/id_ed25519"
    else
        print_sub "$TRAEFIK_SYNC_DIR/id_ed25519 already exists; skipping generation"
    fi
    chmod 600 "$TRAEFIK_SYNC_DIR/id_ed25519"

    if [[ ! -f "$TRAEFIK_SYNC_DIR/id_ed25519.pub" ]]; then
        ssh-keygen -y -f "$TRAEFIK_SYNC_DIR/id_ed25519" > "$TRAEFIK_SYNC_DIR/id_ed25519.pub"
        print_sub "Generated $TRAEFIK_SYNC_DIR/id_ed25519.pub"
    else
        print_sub "$TRAEFIK_SYNC_DIR/id_ed25519.pub already exists; skipping generation"
    fi
    chmod 644 "$TRAEFIK_SYNC_DIR/id_ed25519.pub"
}

refresh_traefik_sync_known_hosts() {
    local config_file="$1"
    local host_name
    local tmp_known_hosts

    host_name=$(awk '$1 == "HostName" { print $2; exit }' "$config_file")
    [[ -z "$host_name" ]] && return 0

    tmp_known_hosts=$(mktemp)
    if ssh-keyscan -H "$host_name" > "$tmp_known_hosts" 2>/dev/null; then
        copy_if_changed "$tmp_known_hosts" "$TRAEFIK_SYNC_DIR/known_hosts" "$TRAEFIK_SYNC_DIR/known_hosts" || true
        chmod 600 "$TRAEFIK_SYNC_DIR/known_hosts"
    else
        if [[ ! -f "$TRAEFIK_SYNC_DIR/known_hosts" ]]; then
            rm -f "$tmp_known_hosts"
            echo "Error: Unable to create $TRAEFIK_SYNC_DIR/known_hosts for $host_name" >&2
            return 1
        fi
        print_warn "Unable to refresh known_hosts for $host_name"
    fi

    rm -f "$tmp_known_hosts"
}

if [[ ! -f "$BUILD_DIR/config" ]]; then
    echo "Error: Missing config at $BUILD_DIR/config"
    exit 1
fi

mkdir -p ~/.ssh
chmod 700 ~/.ssh
if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -f ~/.ssh/config ]] || ! cmp -s "$BUILD_DIR/config" ~/.ssh/config; then
    backup_config ~/.ssh/config
    cp "$BUILD_DIR/config" ~/.ssh/config
    print_sub "Updated ~/.ssh/config"
else
    print_sub "$HOME/.ssh/config unchanged; skipping update"
fi
chmod 600 ~/.ssh/config

if [[ -f "$BUILD_DIR/traefik-sync/config" ]]; then
    mkdir -p "$TRAEFIK_SYNC_ROOT" "$TRAEFIK_SYNC_DIR"
    chmod 700 "$TRAEFIK_SYNC_ROOT" "$TRAEFIK_SYNC_DIR"

    copy_if_changed "$BUILD_DIR/traefik-sync/config" "$TRAEFIK_SYNC_DIR/config" "$TRAEFIK_SYNC_DIR/config" || true
    chmod 600 "$TRAEFIK_SYNC_DIR/config"

    ensure_traefik_sync_key
    refresh_traefik_sync_known_hosts "$BUILD_DIR/traefik-sync/config"

    print_sub "traefik-sync public key available at $TRAEFIK_SYNC_DIR/id_ed25519.pub"
fi
