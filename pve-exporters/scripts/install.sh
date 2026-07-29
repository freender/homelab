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

require_dir "$BUILD_DIR" "$BUILD_DIR" || exit 1
require_file "$BUILD_DIR/file-map.conf" "$BUILD_DIR/file-map.conf" || exit 1
load_file_map "$BUILD_DIR/file-map.conf"

# Install packages only when missing
missing_pkgs=()
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    command -v prometheus-node-exporter &>/dev/null || missing_pkgs+=(prometheus-node-exporter)
    command -v smartctl &>/dev/null              || missing_pkgs+=(smartmontools)
fi
command -v python3 &>/dev/null               || missing_pkgs+=(python3)
command -v curl &>/dev/null                  || missing_pkgs+=(curl)
command -v tar &>/dev/null                   || missing_pkgs+=(tar)
if [[ -n "${FILE_MAP_DEST[igpu-exporter.defaults]:-}" ]]; then
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

if [[ "$EXPORTER_RUNTIME" == "docker" ]]; then
    # Native node-exporter/smartctl-exporter are not managed in docker mode: the
    # host-managed compose stack under /mnt/cache/appdata/<host>-exporters/
    # owns those services instead (see README). Actively remove any leftovers
    # from an earlier native deploy so no unit is left installed-and-disabled
    # pointing at a binary this mode never installs.
    systemctl disable --now prometheus-node-exporter smartctl-exporter 2>/dev/null || true
    systemctl stop prometheus-node-exporter smartctl-exporter 2>/dev/null || true
    systemctl reset-failed prometheus-node-exporter.service smartctl-exporter.service 2>/dev/null || true
    rm -f /etc/default/prometheus-node-exporter /etc/default/smartctl-exporter \
          /etc/systemd/system/smartctl-exporter.service /usr/local/bin/smartctl_exporter
elif [[ -n "${FILE_MAP_DEST[node-exporter.defaults]:-}" ]] && \
     file_needs_update "$BUILD_DIR/node-exporter.defaults" "$(mapped_dest node-exporter.defaults)"; then
    NODE_EXPORTER_CHANGED=true
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

if [[ -z "${FILE_MAP_DEST[apcupsd-exporter.py]:-}" ]]; then
    systemctl disable --now apcupsd-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter
fi

if [[ -z "${FILE_MAP_DEST[igpu-exporter.defaults]:-}" ]]; then
    systemctl disable --now igpu-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/igpu-exporter.service /etc/default/igpu-exporter /usr/local/bin/igpu-exporter
fi

# Install/update every file this host's file-map declares (native node-exporter
# and smartctl-exporter config are simply absent from the map in docker mode;
# apcupsd/igpu config are absent unless those features are configured for the
# host; see pve_exporters.py:build_file_specs).
rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

if [[ -n "${FILE_MAP_DEST[igpu-exporter.defaults]:-}" ]]; then
    # shellcheck source=/etc/default/igpu-exporter
    source /etc/default/igpu-exporter
    IGPU_BIN="/usr/local/bin/igpu-exporter"
    # go version -m's vcs.revision is only populated when the build happens
    # inside a git checkout; ours is built from a tarball extracted from a
    # GitHub archive URL (no .git dir), so vcs.revision is always empty and
    # that check can never detect a match, forcing a rebuild-from-source on
    # every single deploy. Track the built version in a sidecar file instead.
    IGPU_VERSION_FILE="/usr/local/bin/.igpu-exporter.version"
    IGPU_TMP_DIR="$(mktemp -d)"
    IGPU_SOURCE_URL="https://github.com/mike1808/igpu-exporter/archive/${IGPU_EXPORTER_VERSION}.tar.gz"
    installed_igpu_version=""
    if [[ -x "$IGPU_BIN" && -f "$IGPU_VERSION_FILE" ]]; then
        installed_igpu_version="$(cat "$IGPU_VERSION_FILE" 2>/dev/null || true)"
    fi
    if [[ "$FORCE_UPDATE" == "true" ]] || [[ ! -x "$IGPU_BIN" ]] || [[ "$installed_igpu_version" != "$IGPU_EXPORTER_VERSION" ]]; then
        print_sub "Installing igpu-exporter ${IGPU_EXPORTER_VERSION} (was: ${installed_igpu_version:-none})"
        curl -fsSL "$IGPU_SOURCE_URL" -o "$IGPU_TMP_DIR/igpu-exporter.tar.gz"
        tar -xzf "$IGPU_TMP_DIR/igpu-exporter.tar.gz" -C "$IGPU_TMP_DIR"
        systemctl stop igpu-exporter 2>/dev/null || true
        (
            cd "$IGPU_TMP_DIR/igpu-exporter-${IGPU_EXPORTER_VERSION}" &&
            go build -o "$IGPU_BIN" ./cmd
        )
        chmod 755 "$IGPU_BIN"
        printf '%s' "$IGPU_EXPORTER_VERSION" > "$IGPU_VERSION_FILE"
    else
        print_sub "igpu-exporter ${IGPU_EXPORTER_VERSION} already installed"
    fi
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

    # Detect installed version. smartctl_exporter writes --version output to
    # stderr, not stdout (confirmed empirically); redirecting stderr away here
    # silently discards it, leaving installed_version always empty and forcing
    # a redownload+reinstall on every single deploy regardless of whether the
    # binary is already current. Merge stderr into the pipe instead.
    installed_version=""
    if [[ -x "$SMART_BIN" ]]; then
        installed_version=$("$SMART_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)
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
else
    print_sub "Docker exporter compose is host-managed; not modified by this installer"
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
if [[ -n "${FILE_MAP_DEST[igpu-exporter.defaults]:-}" ]]; then
    systemctl enable --now igpu-exporter
    systemctl is-active --quiet igpu-exporter
fi
if [[ -n "${FILE_MAP_DEST[apcupsd-exporter.py]:-}" ]]; then
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
