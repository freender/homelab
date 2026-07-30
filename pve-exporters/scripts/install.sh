#!/bin/bash
# install.sh - Install pve exporters on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}
EXPORTER_RUNTIME=${EXPORTER_RUNTIME:-native}
IGPU_TMP_DIR=""
NODE_EXPORTER_CHANGED=false
SMARTCTL_OVERRIDE_CHANGED=false

cleanup_tmp_dirs() {
    rm -rf "$IGPU_TMP_DIR"
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

# Legacy: this module used to fetch the upstream smartctl_exporter release
# tarball into /usr/local/bin and ship its own smartctl-exporter.service. It is
# now the distro package (see ensure_smartctl_exporter_package). Tear the old
# copy down BEFORE the package is installed: both bind :9633, and the package's
# postinst starts the service, which would fail on the port clash.
remove_legacy_smartctl_exporter() {
    local path
    local removed=false

    if [[ -e /etc/systemd/system/smartctl-exporter.service ]]; then
        systemctl disable --now smartctl-exporter.service 2>/dev/null || true
        systemctl reset-failed smartctl-exporter.service 2>/dev/null || true
        removed=true
    fi

    for path in /etc/systemd/system/smartctl-exporter.service \
                /etc/default/smartctl-exporter \
                /usr/local/bin/smartctl_exporter; do
        [[ -e "$path" ]] || continue
        rm -f "$path"
        removed=true
    done

    if [[ "$removed" == "true" ]]; then
        print_ok "Removed legacy self-managed smartctl-exporter (now distro-packaged)"
    fi
}

# smartctl_exporter is packaged as prometheus-smartctl-exporter, but only in
# <codename>-backports for Debian stable (it is in main on Ubuntu). The backport
# is the same upstream version this module used to download by hand, so apt can
# own the binary and its smartmontools dependency instead of a hand-rolled
# download + version check. Debian backports sets NotAutomatic +
# ButAutomaticUpgrades, so enabling it never pulls anything else in on its own,
# but does keep this package updated once installed.
ensure_smartctl_exporter_package() {
    local codename repo_file="/etc/apt/sources.list.d/debian-backports.sources"
    local staged repo_changed=false already_installed=false

    if [[ ! -r /etc/os-release ]]; then
        print_error "cannot read /etc/os-release; unable to resolve backports suite"
        return 1
    fi
    # shellcheck source=/dev/null
    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
    if [[ -z "$codename" ]]; then
        print_error "VERSION_CODENAME missing from /etc/os-release"
        return 1
    fi

    # Rendered here rather than staged as a config file so the suite always
    # tracks the running release; a hardcoded suite would break at the next
    # Debian major upgrade.
    staged="$(mktemp)"
    cat > "$staged" <<EOF
# Managed by homelab (pve-exporters): provides prometheus-smartctl-exporter,
# which Debian stable ships only via backports.
Types: deb
URIs: http://deb.debian.org/debian/
Suites: ${codename}-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
    if ! cmp -s "$staged" "$repo_file"; then
        install -m 644 "$staged" "$repo_file"
        repo_changed=true
        print_ok "Enabled ${codename}-backports for prometheus-smartctl-exporter"
    fi
    rm -f "$staged"

    dpkg -s prometheus-smartctl-exporter >/dev/null 2>&1 && already_installed=true

    if [[ "$repo_changed" == "true" ]] || [[ "$already_installed" != "true" ]]; then
        apt-get update -qq
    fi

    if [[ "$already_installed" == "true" ]]; then
        print_sub "prometheus-smartctl-exporter already installed"
        remove_legacy_smartctl_exporter
        return 0
    fi

    # Confirm the package is actually installable BEFORE tearing down the
    # self-managed exporter. Otherwise an unavailable backport (suite not yet
    # populated after a Debian major upgrade, mirror issue) would leave the host
    # with no SMART exporter at all instead of failing with the old one intact.
    if ! apt-cache policy prometheus-smartctl-exporter 2>/dev/null | grep -qE '^  Candidate: [^(]'; then
        print_error "prometheus-smartctl-exporter has no installation candidate in ${codename}-backports"
        print_sub "Leaving the existing smartctl exporter untouched; resolve the repo before retrying."
        return 1
    fi

    # Both bind :9633 and the package postinst starts the service, so the old
    # unit has to be gone first.
    remove_legacy_smartctl_exporter
    apt-get install -y -qq -t "${codename}-backports" prometheus-smartctl-exporter
    print_ok "prometheus-smartctl-exporter installed from ${codename}-backports"
}

# Install packages only when missing
missing_pkgs=()
if [[ "$EXPORTER_RUNTIME" != "docker" ]]; then
    command -v prometheus-node-exporter &>/dev/null || missing_pkgs+=(prometheus-node-exporter)
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
    systemctl disable --now prometheus-node-exporter smartctl-exporter smartctl_exporter 2>/dev/null || true
    systemctl stop prometheus-node-exporter smartctl-exporter smartctl_exporter 2>/dev/null || true
    systemctl reset-failed prometheus-node-exporter.service smartctl-exporter.service smartctl_exporter.service 2>/dev/null || true
    rm -f /etc/default/prometheus-node-exporter /etc/default/smartctl-exporter \
          /etc/systemd/system/smartctl-exporter.service /usr/local/bin/smartctl_exporter
    rm -rf /etc/systemd/system/smartctl_exporter.service.d
else
    if [[ -n "${FILE_MAP_DEST[node-exporter.defaults]:-}" ]] && \
       file_needs_update "$BUILD_DIR/node-exporter.defaults" "$(mapped_dest node-exporter.defaults)"; then
        NODE_EXPORTER_CHANGED=true
    fi
    if [[ -n "${FILE_MAP_DEST[smartctl-exporter-override.conf]:-}" ]] && \
       file_needs_update "$BUILD_DIR/smartctl-exporter-override.conf" \
                         "$(mapped_dest smartctl-exporter-override.conf)"; then
        SMARTCTL_OVERRIDE_CHANGED=true
    fi
    # Handles the backports repo, the availability pre-check, and tearing down
    # the retired self-managed exporter in the right order.
    ensure_smartctl_exporter_package
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

if [[ "$EXPORTER_RUNTIME" == "docker" ]]; then
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
    # Packaged unit name is smartctl_exporter (underscore), not the
    # smartctl-exporter (hyphen) this module used to ship.
    systemctl enable --now smartctl_exporter
    if [[ "$FORCE_UPDATE" == "true" || "$SMARTCTL_OVERRIDE_CHANGED" == "true" ]]; then
        systemctl restart smartctl_exporter
    fi
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
    systemctl is-active --quiet smartctl_exporter
fi
