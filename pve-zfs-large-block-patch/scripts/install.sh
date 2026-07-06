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
LOCK_FILE=/run/lock/homelab-pve-patches.lock

ORIGINAL="    my \$cmd = ['zfs', 'send', '-RpvU'];"
PATCHED="    my \$cmd = ['zfs', 'send', '-RpvUL'];"

write_status() {
  local state=$1
  local package_version
  package_version=$(dpkg-query -W -f='${Version}' libpve-storage-perl 2>/dev/null || true)
  {
    printf 'state=%s\n' "${state}"
    printf 'timestamp_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'target=%s\n' "${TARGET}"
    printf 'package=libpve-storage-perl\n'
    printf 'package_version=%s\n' "${package_version}"
    grep -nF "['zfs', 'send'" "${TARGET}" || true
  } > "${STATUS_FILE}"
}

acquire_patch_lock() {
  mkdir -p "$(dirname "${LOCK_FILE}")"
  exec 200>"${LOCK_FILE}"
  if ! flock -n 200; then
    echo "waiting for shared homelab PVE patch lock: ${LOCK_FILE}"
    flock 200
  fi
}

mkdir -p "${STATE_DIR}" "${BACKUP_DIR}"
acquire_patch_lock

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

# Replication and migration tasks run in forked workers, which load this module
# from disk on the next job; no Proxmox service restart is required.
write_status "${state}"

cat "${STATUS_FILE}"
SCRIPT
chmod 0755 "${PATCH_SCRIPT}"

cat > "${APT_HOOK}" <<EOF
// Reapply the homelab ZFS large-block replication patch after package updates.
// Deferred via systemd-run so the reapply runs after the dpkg transaction has
// fully replaced ZFSPoolPlugin.pm and released the shared patch lock, instead
// of racing the in-transaction Post-Invoke (which could leave the file
// unpatched after a libpve-storage-perl upgrade). Falls back to inline if
// systemd-run is unavailable.
DPkg::Post-Invoke {
  "if command -v systemd-run >/dev/null 2>&1 && [ -x ${PATCH_SCRIPT} ]; then systemd-run --collect --unit=homelab-pve-zfs-large-block-patch --on-active=30s ${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-large-block-patch.log 2>&1 || true; elif [ -x ${PATCH_SCRIPT} ]; then ${PATCH_SCRIPT} >/var/log/homelab-pve-zfs-large-block-patch.log 2>&1 || true; fi";
};
EOF
chmod 0644 "${APT_HOOK}"

"${PATCH_SCRIPT}"
