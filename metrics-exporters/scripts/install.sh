#!/bin/bash
# install.sh - Install pve exporters on target host
# Usage: ./scripts/install.sh [hostname]

set -e

HOST=${1:-$(hostname)}
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/build/$HOST"
FORCE_UPDATE=${FORCE_UPDATE:-false}
NODE_EXPORTER_CHANGED=false
SMARTCTL_OVERRIDE_CHANGED=false
IGPU_EXPORTER_CHANGED=false

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

if [[ ! -r /etc/os-release ]]; then
    print_error "cannot read /etc/os-release"
    exit 1
fi
# shellcheck source=/dev/null
OS_ID="$(. /etc/os-release && printf '%s' "${ID:-}")"
# shellcheck source=/dev/null
OS_CODENAME="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
if [[ -z "$OS_ID" || -z "$OS_CODENAME" ]]; then
    print_error "ID/VERSION_CODENAME missing from /etc/os-release"
    exit 1
fi

# node-exporter and smartctl-exporter are host-native on every host now. Where a
# containerised copy used to serve those ports (the retired runtime: docker mode
# on the offsite Ubuntu hosts), it has to be removed from the host-managed
# compose stack first -- otherwise the native units cannot bind :9100/:9633 and
# the package postinst fails half-way through. cadvisor is unaffected and stays
# in compose. Fail before touching anything rather than leaving a half-migrated
# host.
assert_no_conflicting_containers() {
    local name conflicting=()

    command -v docker >/dev/null 2>&1 || return 0
    for name in node-exporter smartctl-exporter; do
        if [[ -n "$(docker ps --quiet --filter "name=^${name}$" 2>/dev/null)" ]]; then
            conflicting+=("$name")
        fi
    done
    [[ ${#conflicting[@]} -eq 0 ]] && return 0

    print_error "container(s) still bound to the native exporter ports: ${conflicting[*]}"
    print_sub "Remove those services from this host's exporters compose file (keep cadvisor),"
    print_sub "run 'docker compose up -d --remove-orphans', then deploy metrics-exporters again."
    return 1
}

assert_no_conflicting_containers || exit 1

# Legacy: this module used to fetch the upstream smartctl_exporter release
# tarball into /usr/local/bin and ship its own smartctl-exporter.service. It is
# now the distro package (see ensure_smartctl_exporter_package). Tear the old
# copy down BEFORE the package is installed: both bind :9633, and the package's
# postinst starts the service, which would fail on the port clash.
remove_legacy_smartctl_exporter() {
    local path
    local removed=false

    if retire_systemd_unit smartctl-exporter.service \
        /etc/systemd/system/smartctl-exporter.service; then
        removed=true
    fi

    for path in /etc/default/smartctl-exporter \
                /usr/local/bin/smartctl_exporter; do
        [[ -e "$path" ]] || continue
        rm -f "$path"
        removed=true
    done

    if [[ "$removed" == "true" ]]; then
        print_ok "Removed legacy self-managed smartctl-exporter (now distro-packaged)"
    fi
}

# smartctl_exporter is packaged as prometheus-smartctl-exporter. Ubuntu carries
# it in the normal archive; Debian stable ships it only in <codename>-backports,
# so the repo is added there and only there. Debian backports sets NotAutomatic
# + ButAutomaticUpgrades, so enabling it never pulls anything else in on its own
# but does keep this package updated once installed.
ensure_backports_repo_if_debian() {
    local repo_file="/etc/apt/sources.list.d/debian-backports.sources"
    local staged

    if [[ "$OS_ID" != "debian" ]]; then
        # Ubuntu (and anything else): package comes from the default archive, so
        # remove a backports file a previous Debian-shaped deploy may have left.
        if [[ -e "$repo_file" ]]; then
            rm -f "$repo_file"
            REPO_CHANGED=true
            print_ok "Removed Debian backports repo (not applicable on $OS_ID)"
        fi
        return 0
    fi

    # Rendered here rather than staged as a config file so the suite always
    # tracks the running release; a hardcoded suite would break at the next
    # Debian major upgrade.
    staged="$(mktemp)"
    cat > "$staged" <<EOF
# Managed by homelab (metrics-exporters): provides prometheus-smartctl-exporter,
# which Debian stable ships only via backports.
Types: deb
URIs: http://deb.debian.org/debian/
Suites: ${OS_CODENAME}-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
    if ! cmp -s "$staged" "$repo_file"; then
        install -m 644 "$staged" "$repo_file"
        REPO_CHANGED=true
        print_ok "Enabled ${OS_CODENAME}-backports for prometheus-smartctl-exporter"
    fi
    rm -f "$staged"
}

ensure_smartctl_exporter_package() {
    local already_installed=false apt_target=()

    REPO_CHANGED=false
    ensure_backports_repo_if_debian || return 1
    [[ "$OS_ID" == "debian" ]] && apt_target=(-t "${OS_CODENAME}-backports")

    dpkg -s prometheus-smartctl-exporter >/dev/null 2>&1 && already_installed=true

    if [[ "$REPO_CHANGED" == "true" ]] || [[ "$already_installed" != "true" ]]; then
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
        print_error "prometheus-smartctl-exporter has no installation candidate"
        print_sub "Leaving the existing smartctl exporter untouched; resolve the repo before retrying."
        return 1
    fi

    # Both bind :9633 and the package postinst starts the service, so the old
    # unit has to be gone first.
    remove_legacy_smartctl_exporter
    apt-get install -y -qq "${apt_target[@]}" prometheus-smartctl-exporter
    print_ok "prometheus-smartctl-exporter installed"
}

# Install packages only when missing
missing_pkgs=()
command -v prometheus-node-exporter &>/dev/null || missing_pkgs+=(prometheus-node-exporter)
# python3 runs apcupsd-exporter and igpu-exporter. Nothing here downloads
# anything any more (smartctl_exporter comes from apt, igpu-exporter is our own
# script), so curl/tar and the golang-go build toolchain are no longer needed.
command -v python3 &>/dev/null               || missing_pkgs+=(python3)
if [[ -n "${FILE_MAP_DEST[igpu-exporter.py]:-}" ]]; then
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

if file_needs_update "$BUILD_DIR/node-exporter.defaults" "$(mapped_dest node-exporter.defaults)"; then
    NODE_EXPORTER_CHANGED=true
fi
# smartctl_exporter is bare-metal only: an unprivileged LXC guest has no disk
# device nodes for smartctl to probe, so the guest's file map omits the override
# and nothing here runs.
if [[ -n "${FILE_MAP_DEST[smartctl-exporter-override.conf]:-}" ]]; then
    if file_needs_update "$BUILD_DIR/smartctl-exporter-override.conf" \
                         "$(mapped_dest smartctl-exporter-override.conf)"; then
        SMARTCTL_OVERRIDE_CHANGED=true
    fi
    # Handles the backports repo (Debian only), the availability pre-check, and
    # tearing down the retired self-managed exporter in the right order.
    ensure_smartctl_exporter_package
fi

# The exporter script and its env both feed the running process, so either one
# changing needs a restart; the unit itself is handled by daemon-reload.
if [[ -n "${FILE_MAP_DEST[igpu-exporter.py]:-}" ]]; then
    if file_needs_update "$BUILD_DIR/igpu-exporter.py" "$(mapped_dest igpu-exporter.py)" ||
       file_needs_update "$BUILD_DIR/igpu-exporter.defaults" "$(mapped_dest igpu-exporter.defaults)"; then
        IGPU_EXPORTER_CHANGED=true
    fi
fi

# Retired: this textfile fallback only ever existed because the containerized
# node_exporter on cinci/cottonwood could not reach dbus and so could not use
# its native systemd collector. Those containers now bind-mount the host D-Bus
# system bus socket at /var/run/dbus/system_bus_socket, so every host uses the
# real collector (node_systemd_units) and the fallback is removed wherever it
# was previously installed.
if [[ -e /usr/local/bin/systemd-failed-textfile-exporter ]] ||
   [[ -e /etc/systemd/system/systemd-failed-textfile-exporter.timer ]]; then
    # Status discarded: the enclosing guard already established that at least
    # one artifact exists, and either unit may legitimately be absent.
    retire_systemd_unit systemd-failed-textfile-exporter.timer \
        /etc/systemd/system/systemd-failed-textfile-exporter.timer || true
    retire_systemd_unit systemd-failed-textfile-exporter.service \
        /etc/systemd/system/systemd-failed-textfile-exporter.service || true
    rm -f /usr/local/bin/systemd-failed-textfile-exporter \
          /var/lib/prometheus/node-exporter/systemd-failed.prom
    print_sub "Removed retired systemd-failed-textfile-exporter"
fi

if [[ -z "${FILE_MAP_DEST[apcupsd-exporter.py]:-}" ]]; then
    systemctl disable --now apcupsd-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/apcupsd-exporter.service /etc/default/apcupsd-exporter /usr/local/bin/apcupsd-exporter
fi

if [[ -z "${FILE_MAP_DEST[igpu-exporter.py]:-}" ]]; then
    systemctl disable --now igpu-exporter 2>/dev/null || true
    rm -f /etc/systemd/system/igpu-exporter.service /etc/default/igpu-exporter \
          /usr/local/bin/igpu-exporter /usr/local/bin/.igpu-exporter.version
fi

# Legacy: this exporter shipped for one day as hba-temp-textfile-exporter,
# before it also grew SAS PHY link counters and the "temp" in the name stopped
# being true. Retire the old unit unconditionally -- leaving it would keep a
# second timer writing a stale hba-temp.prom that the textfile collector would
# happily keep serving alongside the new one.
if [[ -e /etc/systemd/system/hba-temp-textfile-exporter.timer ]] ||
   [[ -e /usr/local/bin/hba-temp-textfile-exporter ]]; then
    retire_systemd_unit hba-temp-textfile-exporter.timer \
        /etc/systemd/system/hba-temp-textfile-exporter.timer || true
    retire_systemd_unit hba-temp-textfile-exporter.service \
        /etc/systemd/system/hba-temp-textfile-exporter.service || true
    rm -f /usr/local/bin/hba-temp-textfile-exporter \
          /var/lib/prometheus/node-exporter/hba-temp.prom
    print_sub "Removed superseded hba-temp-textfile-exporter (now hba-textfile-exporter)"
fi

# Drop the HBA exporter on hosts that no longer declare it, and take its stale
# .prom with it -- the textfile collector keeps serving whatever is in that
# directory, so a leftover file would report a temperature forever.
if [[ -z "${FILE_MAP_DEST[hba-textfile-exporter.py]:-}" ]]; then
    if [[ -e /etc/systemd/system/hba-textfile-exporter.timer ]]; then
        retire_systemd_unit hba-textfile-exporter.timer \
            /etc/systemd/system/hba-textfile-exporter.timer || true
        retire_systemd_unit hba-textfile-exporter.service \
            /etc/systemd/system/hba-textfile-exporter.service || true
        rm -f /usr/local/bin/hba-textfile-exporter \
              /var/lib/prometheus/node-exporter/hba.prom
        print_sub "Removed hba-textfile-exporter"
    fi
fi

# Same treatment for the disk-label exporter: drop it on a host that no longer
# gets it (an LXC guest), and take its stale .prom with it. The textfile
# collector serves whatever is in that directory regardless of whether anything
# still writes it, so a leftover file would keep naming disks that this host may
# no longer have.
if [[ -z "${FILE_MAP_DEST[disk-label-textfile-exporter.py]:-}" ]]; then
    if [[ -e /etc/systemd/system/disk-label-textfile-exporter.timer ]]; then
        retire_systemd_unit disk-label-textfile-exporter.timer \
            /etc/systemd/system/disk-label-textfile-exporter.timer || true
        retire_systemd_unit disk-label-textfile-exporter.service \
            /etc/systemd/system/disk-label-textfile-exporter.service || true
        rm -f /usr/local/bin/disk-label-textfile-exporter \
              /var/lib/prometheus/node-exporter/disk-labels.prom
        print_sub "Removed disk-label-textfile-exporter"
    fi
fi

# The per-model override file is optional and independent of the exporter: a
# host that stops declaring metrics-exporters.disk_labels must lose the file,
# or the exporter would keep applying overrides that hosts.conf no longer says.
if [[ -z "${FILE_MAP_DEST[disk-labels.conf]:-}" ]] && [[ -e /etc/homelab/disk-labels.conf ]]; then
    rm -f /etc/homelab/disk-labels.conf
    print_sub "Removed disk-labels.conf (no overrides configured)"
fi

# PVE patch statuses are optional: hosts without any managed source patch must
# not retain an old manifest or textfile metric from a previous configuration.
if [[ -z "${FILE_MAP_DEST[pve-patch-statuses.conf]:-}" ]]; then
    if [[ -e /etc/homelab/pve-patch-statuses.conf ]] || \
       [[ -e /var/lib/prometheus/node-exporter/pve-patches.prom ]]; then
        rm -f /etc/homelab/pve-patch-statuses.conf \
              /var/lib/prometheus/node-exporter/pve-patches.prom
        print_sub "Removed pve patch status manifest and metrics (no patches configured)"
    fi
fi

# Install/update every file this host's file-map declares (native node-exporter
# and smartctl-exporter config are simply absent from the map in docker mode;
# apcupsd/igpu config are absent unless those features are configured for the
# host; see metrics_exporters.py:build_file_specs).
rc=0
install_file_map "$BUILD_DIR" || rc=$?
[[ $rc -eq 0 || $rc -eq 1 ]] || exit "$rc"

if [[ -n "${FILE_MAP_DEST[igpu-exporter.py]:-}" ]]; then
    # Retired: igpu-exporter used to be the third-party Go exporter, compiled
    # from a pinned git revision here because upstream ships no releases and it
    # is packaged nowhere. /usr/local/bin/igpu-exporter is now our own Python
    # script (installed via the file map, same path), so the build toolchain and
    # its version sidecar are no longer needed.
    if [[ -e /usr/local/bin/.igpu-exporter.version ]]; then
        rm -f /usr/local/bin/.igpu-exporter.version
        print_ok "Removed retired igpu-exporter build-version sidecar"
    fi
fi

# This module owns failed-unit *visibility* (it retired the textfile fallback in
# favour of node_exporter's systemd collector), so it also keeps that signal
# clean. openipmi is an LSB init script that tries to load IPMI kernel modules;
# it can never succeed on a host with no BMC, and never inside an LXC guest,
# where it leaves a permanently-failed unit that masks real failures in
# SystemdUnitFailed. Masking here covers hosts that get neither pve-postinstall
# nor ubuntu-setup (helm/neo/tower); it is idempotent where those already ran.
print_action "Unwanted default services"
homelab_mask_unwanted_service openipmi.service

# Same principle, found when xur/arc/deepstone were first scraped on 2026-08-15:
# turning failed-unit visibility on for a host that never had it exposes units
# that have been failed for weeks and can never succeed, which is pure noise in
# SystemdUnitFailed. Both checks below are runtime facts, not host lists, so a
# bare-metal host can never trip them:
#   - nvmf-autoconnect needs the nvme-fabrics module, which a container cannot
#     load (deepstone).
#   - the ZFS units are gated on /dev/zfs being absent, which is the definition
#     of "this host cannot do ZFS". On xur they had been failed since 2026-08-07.
#     Guarding on the device rather than on container-ness means masking cannot
#     fire on a host that actually mounts ZFS.
if systemd-detect-virt --container --quiet; then
    homelab_mask_unwanted_service nvmf-autoconnect.service "no nvme-fabrics in a container"
    if [[ ! -e /dev/zfs ]]; then
        homelab_mask_unwanted_service zfs-mount.service "no /dev/zfs"
        homelab_mask_unwanted_service zfs-share.service "no /dev/zfs"
        homelab_mask_unwanted_service zfs-zed.service "no /dev/zfs"
    fi
fi

systemctl daemon-reload
systemctl enable --now prometheus-node-exporter
if [[ "$FORCE_UPDATE" == "true" || "$NODE_EXPORTER_CHANGED" == "true" ]]; then
    systemctl restart prometheus-node-exporter
fi
if [[ -n "${FILE_MAP_DEST[zfs-pool-textfile-exporter]:-}" ]]; then
    systemctl enable --now zfs-pool-textfile-exporter.timer
    systemctl start zfs-pool-textfile-exporter.service
fi
if [[ -n "${FILE_MAP_DEST[hba-textfile-exporter.py]:-}" ]]; then
    systemctl enable --now hba-textfile-exporter.timer
    systemctl start hba-textfile-exporter.service
fi
# Same bare-metal gate as the ZFS exporter above: absent from an LXC guest's
# file map, so nothing here runs there.
if [[ -n "${FILE_MAP_DEST[reboot-textfile-exporter]:-}" ]]; then
    systemctl enable --now reboot-textfile-exporter.timer
    systemctl start reboot-textfile-exporter.service
fi
# Bare-metal gate again. Started explicitly as well as enabled so a deploy that
# changes the naming rules (or the override file) is reflected immediately
# rather than at the next 5-minute tick.
if [[ -n "${FILE_MAP_DEST[disk-label-textfile-exporter.py]:-}" ]]; then
    systemctl enable --now disk-label-textfile-exporter.timer
    systemctl start disk-label-textfile-exporter.service
fi
if [[ -n "${FILE_MAP_DEST[smartctl-exporter-override.conf]:-}" ]]; then
    # Packaged unit name is smartctl_exporter (underscore), not the
    # smartctl-exporter (hyphen) this module used to ship.
    systemctl enable --now smartctl_exporter
    if [[ "$FORCE_UPDATE" == "true" || "$SMARTCTL_OVERRIDE_CHANGED" == "true" ]]; then
        systemctl restart smartctl_exporter
    fi
fi
if [[ -n "${FILE_MAP_DEST[igpu-exporter.py]:-}" ]]; then
    systemctl enable --now igpu-exporter
    if [[ "$FORCE_UPDATE" == "true" || "$IGPU_EXPORTER_CHANGED" == "true" ]]; then
        systemctl restart igpu-exporter
    fi
    systemctl is-active --quiet igpu-exporter
fi
if [[ -n "${FILE_MAP_DEST[apcupsd-exporter.py]:-}" ]]; then
    systemctl enable --now apcupsd-exporter
    systemctl is-active --quiet apcupsd-exporter
fi
systemctl is-active --quiet prometheus-node-exporter
if [[ -n "${FILE_MAP_DEST[zfs-pool-textfile-exporter]:-}" ]]; then
    systemctl is-active --quiet zfs-pool-textfile-exporter.timer
fi
if [[ -n "${FILE_MAP_DEST[hba-textfile-exporter.py]:-}" ]]; then
    systemctl is-active --quiet hba-textfile-exporter.timer
fi
if [[ -n "${FILE_MAP_DEST[reboot-textfile-exporter]:-}" ]]; then
    systemctl is-active --quiet reboot-textfile-exporter.timer
fi
if [[ -n "${FILE_MAP_DEST[disk-label-textfile-exporter.py]:-}" ]]; then
    systemctl is-active --quiet disk-label-textfile-exporter.timer
fi
if [[ -n "${FILE_MAP_DEST[smartctl-exporter-override.conf]:-}" ]]; then
    systemctl is-active --quiet smartctl_exporter
fi
