#!/usr/bin/env bash
set -euo pipefail

STATE_DIR=/var/lib/homelab/pve-zfs-large-block-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-large-block-patch
PATCH_SCRIPT=/usr/local/sbin/homelab-pve-zfs-large-block-patch
APT_HOOK=/etc/apt/apt.conf.d/99-homelab-pve-zfs-large-block-patch

if [[ ${EUID} -ne 0 ]]; then
  echo "must run as root" >&2
  exit 1
fi

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

cat > "${PATCH_SCRIPT}" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

TARGET=/usr/share/perl5/PVE/Storage/ZFSPoolPlugin.pm
STATE_DIR=/var/lib/homelab/pve-zfs-large-block-patch
BACKUP_DIR=/var/backups/homelab/pve-zfs-large-block-patch
STATUS_FILE=${STATE_DIR}/status
PVE_SERVICES=(pvescheduler pvedaemon pvestatd)

ORIGINAL="    my \$cmd = ['zfs', 'send', '-RpvU'];"
PATCHED="    my \$cmd = ['zfs', 'send', '-RpvUL'];"

write_status() {
  local state=$1
  local restart_state=${2:-not-run}
  local package_version
  package_version=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
  {
    printf 'state=%s\n' "${state}"
    printf 'services_restart=%s\n' "${restart_state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target=%s\n' "${TARGET}"
    printf 'package=libpve-storage-perl\n'
    printf 'package_version=%s\n' "${package_version}"
    grep -nF "['zfs', 'send'" "${TARGET}" || true
  } > "${STATUS_FILE}"
}

restart_pve_services() {
  # Proxmox Perl daemons keep modules loaded; restart them so ZFSPoolPlugin.pm
  # changes affect scheduled replication/migration immediately after deploy or apt hooks.
  systemctl try-restart "${PVE_SERVICES[@]}"
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"

if [[ ! -f ${TARGET} ]]; then
  echo "missing target: ${TARGET}" >&2
  {
    printf 'state=failed-missing-target\n'
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target=%s\n' "${TARGET}"
  } > "${STATUS_FILE}"
  exit 1
fi

patched_count=$(grep -Fc "${PATCHED}" "${TARGET}" || true)
original_count=$(grep -Fc "${ORIGINAL}" "${TARGET}" || true)

if [[ ${patched_count} -eq 1 && ${original_count} -eq 0 ]]; then
  state=already-patched
elif [[ ${patched_count} -eq 0 && ${original_count} -eq 1 ]]; then
  backup="${BACKUP_DIR}/ZFSPoolPlugin.pm.$(date -u +%Y%m%dT%H%M%SZ).bak"
  cp "${TARGET}" "${backup}"
  PATCHED_LINE=${PATCHED} ORIGINAL_LINE=${ORIGINAL} perl -0pi -e 's/\Q$ENV{ORIGINAL_LINE}\E/$ENV{PATCHED_LINE}/' "${TARGET}"
  state=patched
else
  echo "unexpected ZFS send line in ${TARGET}; refusing to patch" >&2
  grep -nF "['zfs', 'send'" "${TARGET}" >&2 || true
  write_status failed-unexpected-line
  exit 1
fi

restart_state=restarted
if ! restart_pve_services; then
  restart_state=failed
  write_status "${state}" "${restart_state}"
  echo "failed to restart Proxmox services: ${PVE_SERVICES[*]}" >&2
  exit 1
fi

write_status "${state}" "${restart_state}"

cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS large-block replication patch after package updates.
DPkg::Post-Invoke { "${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-large-block-patch.log 2>&1 || true"; };
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
