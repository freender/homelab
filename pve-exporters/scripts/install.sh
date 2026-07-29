#!/bin/bash
# install.sh - Install pve exporters on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}
EXPORTER_RUNTIME=${EXPORTER_RUNTIME:-native}
TMP_DIR=""
IGPU_TMP_DIR=""
NODE_EXPORTER_CHANGED=false

cleanup_tmp_dirs() {
    rm -rf "$TMP_DIR" "$IGPU_TMP_DIR"
}

trap cleanup_tmp_dirs EXIT

if [[ -f "$SCRIPT_DIR/lib/utils.sh" ]]; then
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/lib/utils.sh"
else
    echo "Error: Missing shared utils at $SCRIPT_DIR/lib/utils.sh" >&2
    exit 1
fi

require_dir "$BUILD_DIR/configs" "$BUILD_DIR/configs" || exit 1

NODE_ENV_SRC="$BUILD_DIR/configs/node-exporter.defaults"
SMART_ENV_SRC="$BUILD_DIR/configs/smartctl-exporter.defaults"
SMART_SVC_SRC="$BUILD_DIR/configs/smartctl-exporter.service"
ZFS_POOL_BIN_SRC="$BUILD_DIR/configs/zfs-pool-textfile-exporter"
ZFS_POOL_SVC_SRC="$BUILD_DIR/configs/zfs-pool-textfile-exporter.service"
ZFS_POOL_TIMER_SRC="$BUILD_DIR/configs/zfs-pool-textfile-exporter.timer"
ZFS_EXPECTED_POOLS_SRC="$BUILD_DIR/configs/zfs-expected-pools.conf"
APC_BIN_SRC="$BUILD_DIR/configs/apcupsd-exporter.py"
APC_ENV_SRC="$BUILD_DIR/configs/apcupsd-exporter.env"
APC_SVC_SRC="$BUILD_DIR/configs/apcupsd-exporter.service"
IGPU_ENV_SRC="$BUILD_DIR/configs/igpu-exporter.defaults"
IGPU_SVC_SRC="$BUILD_DIR/configs/igpu-exporter.service"

# Install packages only when missing
missing_pkgs=()
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    command -v prometheus-node-exporter &>/dev/null || missing_pkgs+=(prometheus-node-exporter)
    command -v smartctl &>/dev/null              || missing_pkgs+=(smartmontools)
fi
command -v python3 &>/dev/null               || missing_pkgs+=(python3)
command -v curl &>/dev/null                  || missing_pkgs+=(curl)
command -v tar &>/dev/null                   || missing_pkgs+=(tar)
if [[ -f "$IGPU_ENV_SRC" && -f "$IGPU_SVC_SRC" ]]; then
    command -v go &>/dev/null                || missing_pkgs+=(golang-go)
    command -v intel_gpu_top &>/dev/null     || missing_pkgs+=(intel-gpu-tools)
fi
if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
    print_sub "Installing packages: ${missing_pkgs[*]}"
    apt-get update -qq
    apt-get install -y -qq "${missing_pkgs[@]}"
else
    print_sub "All required packages already installed"
fi

mkdir -p /etc/default
mkdir -p /var/lib/prometheus/node-exporter
rc=0
backup_and_copy_if_changed "$NODE_ENV_SRC" /etc/default/prometheus-node-exporter || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
[[ $rc -eq 0 ]] && NODE_EXPORTER_CHANGED=true

rc=0
backup_and_copy_if_changed "$SMART_ENV_SRC" /etc/default/smartctl-exporter || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

rc=0
backup_and_copy_if_changed "$SMART_SVC_SRC" /etc/systemd/system/smartctl-exporter.service || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

if [[ "$EXPORTER_RUNTIME" == "docker" ]]; then
    systemctl disable --now prometheus-node-exporter smartctl-exporter 2>/dev/null || true
    systemctl stop prometheus-node-exporter smartctl-exporter 2>/dev/null || true
    systemctl reset-failed prometheus-node-exporter.service smartctl-exporter.service 2>/dev/null || true
fi

if file_needs_update "$ZFS_POOL_BIN_SRC" /usr/local/bin/zfs-pool-textfile-exporter; then
    backup_config /usr/local/bin/zfs-pool-textfile-exporter
    install -m 755 "$ZFS_POOL_BIN_SRC" /usr/local/bin/zfs-pool-textfile-exporter
    print_sub "Updated zfs-pool-textfile-exporter"
else
    print_sub "zfs-pool-textfile-exporter unchanged; skipping update"
fi

rc=0
backup_and_copy_if_changed "$ZFS_POOL_SVC_SRC" /etc/systemd/system/zfs-pool-textfile-exporter.service || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

rc=0
backup_and_copy_if_changed "$ZFS_POOL_TIMER_SRC" /etc/systemd/system/zfs-pool-textfile-exporter.timer || rc=$?
[[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

# Only staged when the host sets pve-exporters.zfs_expected_pools. Absent file
# means the exporter reports only imported pools, which is the pre-existing
# behaviour.
if [[ -f "$ZFS_EXPECTED_POOLS_SRC" ]]; then
    mkdir -p /etc/homelab
    rc=0
    backup_and_copy_if_changed "$ZFS_EXPECTED_POOLS_SRC" /etc/homelab/zfs-expected-pools.conf || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
    chmod 0644 /etc/homelab/zfs-expected-pools.conf
fi

# Retired: this textfile fallback only ever existed because the containerized
# node_exporter on cinci/cottonwood could not reach dbus and so could not use
# its native systemd collector. Those containers now bind-mount the host D-Bus
# system bus socket at /var/run/dbus/system_bus_socket, so every host uses the
# real collector (node_systemd_units) and the fallback is removed wherever it
# was previously installed.
if [[ -e /usr/local/bin/systemd-failed-textfile-exporter ]] ||
   [[ -e /etc/systemd/system/systemd-failed-textfile-exporter.timer ]]; then
    systemctl disable --now systemd-failed-textfile-exporter.timer 2>/dev/null || true
    systemctl stop systemd-failed-textfile-exporter.service 2>/dev/null || true
    systemctl reset-failed systemd-failed-textfile-exporter.service 2>/dev/null || true
    rm -f /etc/systemd/system/systemd-failed-textfile-exporter.service \
          /etc/systemd/system/systemd-failed-textfile-exporter.timer \
          /usr/local/bin/systemd-failed-textfile-exporter \
          /var/lib/prometheus/node-exporter/systemd-failed.prom
    print_sub "Removed retired systemd-failed-textfile-exporter"
fi

if [[ -f "$APC_BIN_SRC" && -f "$APC_ENV_SRC" && -f "$APC_SVC_SRC" ]]; then
    if file_needs_update "$APC_BIN_SRC" /usr/local/bin/apcupsd-exporter; then
        backup_config /usr/local/bin/apcupsd-exporter
        install -m 755 "$APC_BIN_SRC" /usr/local/bin/apcupsd-exporter
        print_sub "Updated apcupsd-exporter"
    else
        print_sub "apcupsd-exporter unchanged; skipping update"
    fi
    rc=0
    backup_and_copy_if_changed "$APC_ENV_SRC" /etc/default/apcupsd-exporter || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

    rc=0
    backup_and_copy_if_changed "$APC_SVC_SRC" /etc/systemd/system/apcupsd-exporter.service || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"
else
    systemctl disable --now apcupsd-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter
fi

if [[ -f "$IGPU_ENV_SRC" && -f "$IGPU_SVC_SRC" ]]; then
    rc=0
    backup_and_copy_if_changed "$IGPU_ENV_SRC" /etc/default/igpu-exporter || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

    rc=0
    backup_and_copy_if_changed "$IGPU_SVC_SRC" /etc/systemd/system/igpu-exporter.service || rc=$?
    [[ $rc -eq 1 ]] || [[ $rc -eq 0 ]] || exit "$rc"

    # shellcheck source=/etc/default/igpu-exporter
    source /etc/default/igpu-exporter
    IGPU_BIN="/usr/local/bin/igpu-exporter"
    IGPU_TMP_DIR="$(mktemp -d)"
    IGPU_SOURCE_URL="https://github.com/mike1808/igpu-exporter/archive/${IGPU_EXPORTER_VERSION}.tar.gz"
    installed_igpu_version=""
    if [[ -x "$IGPU_BIN" ]]; then
        installed_igpu_version=$(go version -m "$IGPU_BIN" 2>/dev/null | sed -n 's/^\s*vcs.revision=//p' | head -1 || true)
    fi
    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -x "$IGPU_BIN" ]] || [[ "$installed_igpu_version" != "$IGPU_EXPORTER_VERSION" ]]; then
        print_sub "Installing igpu-exporter ${IGPU_EXPORTER_VERSION}"
        curl -fsSL "$IGPU_SOURCE_URL" -o "$IGPU_TMP_DIR/igpu-exporter.tar.gz"
        tar -xzf "$IGPU_TMP_DIR/igpu-exporter.tar.gz" -C "$IGPU_TMP_DIR"
        systemctl stop igpu-exporter 2>/dev/null || true
        (
            cd "$IGPU_TMP_DIR/igpu-exporter-${IGPU_EXPORTER_VERSION}" &&
            go build -o "$IGPU_BIN" ./cmd
        )
        chmod 755 "$IGPU_BIN"
    else
        print_sub "igpu-exporter ${IGPU_EXPORTER_VERSION} already installed"
    fi
else
    systemctl disable --now igpu-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/igpu-exporter.service /etc/default/igpu-exporter /usr/local/bin/igpu-exporter
fi

if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    # Install smartctl_exporter binary (version-aware)
    # shellcheck source=/etc/default/smartctl-exporter
    source /etc/default/smartctl-exporter
    ARCH="$(dpkg --print-architecture)"
    case "$ARCH" in
        amd64) ARCH_TAG="amd64" ;;
        arm64) ARCH_TAG="arm64" ;;
        *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
    esac

    SMART_BIN="/usr/local/bin/smartctl_exporter"
    SMART_URL="https://github.com/prometheus-community/smartctl_exporter/releases/download/v${SMARTCTL_EXPORTER_VERSION}/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-${ARCH_TAG}.tar.gz"
    TMP_DIR="$(mktemp -d)"

    # Detect installed version
    installed_version=""
    if [[ -x "$SMART_BIN" ]]; then
        installed_version=$("$SMART_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
    fi

    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -x "$SMART_BIN" ]] || [[ "$installed_version" != "$SMARTCTL_EXPORTER_VERSION" ]]; then
        print_sub "Installing smartctl_exporter v${SMARTCTL_EXPORTER_VERSION} (was: ${installed_version:-none})"
        curl -fsSL "$SMART_URL" -o "$TMP_DIR/smartctl-exporter.tar.gz"
        tar -xzf "$TMP_DIR/smartctl-exporter.tar.gz" -C "$TMP_DIR"
        # Stop service before replacing binary to avoid "Text file busy"
        systemctl stop smartctl-exporter 2>/dev/null || true
        cp "$TMP_DIR/smartctl_exporter-${SMARTCTL_EXPORTER_VERSION}.linux-${ARCH_TAG}/smartctl_exporter" "$SMART_BIN"
        chmod 755 "$SMART_BIN"
    else
        print_sub "smartctl_exporter v${SMARTCTL_EXPORTER_VERSION} already installed"
    fi
fi

if [[ "$EXPORTER_RUNTIME" == "docker" ]]; then
    compose_file="/mnt/cache/appdata/exporters/compose.yml"
    if [[ ! -f "$compose_file" && -f "/mnt/cache/appdata/${HOST}-exporters/compose.yml" ]]; then
        compose_file="/mnt/cache/appdata/${HOST}-exporters/compose.yml"
    fi
    if [[ -f "$compose_file" ]]; then
        python3 - "$compose_file" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
if "--collector.textfile.directory=/host/var/lib/prometheus/node-exporter" not in text:
    lines = text.splitlines(keepends=True)
    updated = []
    inserted = False
    for line in lines:
        updated.append(line)
        if not inserted and ("--collector.zfs" in line or "--collector.filesystem" in line):
            prefix = line.split("-")[0]
            stripped = line.strip()
            quote = stripped[2] if len(stripped) > 2 and stripped[2] in {"'", '"'} else ""
            updated.append(f"{prefix}- {quote}--collector.textfile{quote}\n")
            updated.append(
                f"{prefix}- {quote}--collector.textfile.directory=/host/var/lib/prometheus/node-exporter{quote}\n"
            )
            inserted = True
    text = "".join(updated)
    path.write_text(text, encoding="utf-8")
PY
        node_service="$(cd "$(dirname "$compose_file")" && docker compose config --services | grep -E '^node-exporter(-.*)?$' | head -1)"
        if [[ -z "$node_service" ]]; then
            echo "Could not find node-exporter service in $compose_file" >&2
            exit 1
        fi
        (cd "$(dirname "$compose_file")" && docker compose up -d "$node_service")
    else
        print_sub "Docker exporter compose not found: $compose_file"
    fi
fi

systemctl daemon-reload
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    systemctl enable --now prometheus-node-exporter
    if [[ "$FORCE_UPDATE" == "true" || "$NODE_EXPORTER_CHANGED" == "true" ]]; then
        systemctl restart prometheus-node-exporter
    fi
fi
systemctl enable --now zfs-pool-textfile-exporter.timer
systemctl start zfs-pool-textfile-exporter.service
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    systemctl enable --now smartctl-exporter
fi
if [[ -f "$IGPU_ENV_SRC" && -f "$IGPU_SVC_SRC" ]]; then
    systemctl enable --now igpu-exporter
    systemctl is-active --quiet igpu-exporter
fi
if [[ -f "$APC_BIN_SRC" && -f "$APC_ENV_SRC" && -f "$APC_SVC_SRC" ]]; then
    systemctl enable --now apcupsd-exporter
    systemctl is-active --quiet apcupsd-exporter
fi
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    systemctl is-active --quiet prometheus-node-exporter
fi
systemctl is-active --quiet zfs-pool-textfile-exporter.timer
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    systemctl is-active --quiet smartctl-exporter
fi
